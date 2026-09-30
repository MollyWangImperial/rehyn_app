// Web replacement for react-native-webview (aliased in metro.config.js when platform === "web").
// Renders the URL in an <iframe> and bridges the runner page's
// `window.ReactNativeWebView.postMessage(...)` calls back to the `onMessage` prop.
//
// Bridge strategy (in order):
//   1. Same-origin iframe (backend proxied through the site origin, see vercel.json):
//      inject a ReactNativeWebView object into the iframe's contentWindow so the
//      runner's existing `if (window.ReactNativeWebView) ...` code just works.
//   2. window "message" events (in case the runner ever posts to parent directly).
import React, { forwardRef, useEffect, useImperativeHandle, useRef } from "react";
import { View, StyleSheet } from "react-native";

export type WebViewMessageEvent = { nativeEvent: { data: string } };

type Props = {
  source?: { uri?: string; html?: string };
  style?: any;
  onMessage?: (e: WebViewMessageEvent) => void;
  onLoadEnd?: () => void;
  onError?: (e: any) => void;
  injectedJavaScript?: string;
  testID?: string;
  [key: string]: any; // ignore native-only props
};

export const WebView = forwardRef<any, Props>(function WebViewWeb(
  { source, style, onMessage, onLoadEnd, onError, injectedJavaScript, testID },
  ref,
) {
  const iframeRef = useRef<HTMLIFrameElement | null>(null);
  const onMessageRef = useRef(onMessage);
  onMessageRef.current = onMessage;

  useImperativeHandle(ref, () => ({
    postMessage: (data: string) => {
      try {
        iframeRef.current?.contentWindow?.postMessage(data, "*");
      } catch {}
    },
    injectJavaScript: (js: string) => {
      try {
        // Same-origin only.
        (iframeRef.current?.contentWindow as any)?.eval(js);
      } catch {}
    },
    reload: () => {
      try {
        const el = iframeRef.current;
        if (el) el.src = el.src; // eslint-disable-line no-self-assign
      } catch {}
    },
  }));

  // Bridge 1: inject ReactNativeWebView into a same-origin iframe.
  useEffect(() => {
    const timer = setInterval(() => {
      try {
        const win = iframeRef.current?.contentWindow as any;
        if (win && !win.ReactNativeWebView) {
          win.ReactNativeWebView = {
            postMessage: (data: string) => {
              // Dispatch on the parent event loop. Completion unmounts the
              // iframe; Firefox must not associate the parent's score fetch
              // with the child document that is about to be destroyed.
              window.setTimeout(() => onMessageRef.current?.({ nativeEvent: { data } }), 0);
            },
          };
        }
      } catch {
        // Cross-origin iframe — rely on bridge 2.
      }
    }, 150);
    return () => clearInterval(timer);
  }, [source?.uri]);

  // Bridge 2: listen for messages posted to the parent window.
  useEffect(() => {
    const handler = (ev: MessageEvent) => {
      try {
        if (iframeRef.current && ev.source === iframeRef.current.contentWindow) {
          const data = typeof ev.data === "string" ? ev.data : JSON.stringify(ev.data);
          onMessageRef.current?.({ nativeEvent: { data } });
        }
      } catch {}
    };
    window.addEventListener("message", handler);
    return () => window.removeEventListener("message", handler);
  }, []);

  const handleLoad = () => {
    try {
      const win = iframeRef.current?.contentWindow as any;
      if (win && injectedJavaScript) win.eval(injectedJavaScript);
    } catch {}
    onLoadEnd?.();
  };

  return (
    <View style={[styles.container, style]} testID={testID}>
      <iframe
        ref={iframeRef}
        src={source?.uri}
        srcDoc={source?.html}
        onLoad={handleLoad}
        onError={(e) => onError?.({ nativeEvent: { description: String(e) } })}
        allow="camera; microphone; autoplay; fullscreen; display-capture"
        allowFullScreen
        style={{ border: 0, width: "100%", height: "100%", display: "block", background: "transparent" }}
      />
    </View>
  );
});

export default WebView;

const styles = StyleSheet.create({
  container: {
    flex: 1,
    width: "100%",
    height: "100%",
  },
});
