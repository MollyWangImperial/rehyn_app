"""Authored Molly cues for T3 Testing; private audio is generated locally."""

CALIBRATION = "Sit comfortably upright with your affected hand resting on your lap. Keep your face, shoulders, affected arm and the top of your thighs visible. Hold still while we find your starting position."
COMPLETE = "Calibration complete. Stay seated in this position and do not move the camera. We will begin the assessment now."
STEPS = (
    "Let's bring your affected hand toward your mouth. First, lift it from your lap toward the circle at your chest.",
    "Now, slowly bring your hand up to your mouth, as if you were drinking from a cup. Keep your head still.",
    "Once your hand reaches the circle at your mouth, keep it there for a moment. Do not lower it until I ask you to return to your lap.",
    "Great job. Lower your affected hand back to your lap.",
)
NEAR = (
    "You're close. Move your hand slightly farther toward the center of the circle, then keep it still there.",
    "You're close. Move your hand slightly away, then deliberately reach back into the center of the circle and hold it there.",
    "You're close. Keep the instructed body part in the center of the circle and hold it still until the ring completes.",
)
LINES = (CALIBRATION, COMPLETE, *STEPS, *NEAR)
