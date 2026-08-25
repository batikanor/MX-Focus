// Turns a focus score into a pen-position offset.
//
// Three deliberate departures from the original implementation, which added
// white noise on all three axes to 50% of points whenever calmness fell below
// a hardcoded 0.45:
//
//   1. Graded, not a cliff. Amplitude scales with (1 - focus), so the student
//      feels themselves drifting. A threshold means nothing happens until
//      suddenly everything does, which teaches nothing about the approach to
//      distraction -- only about its arrival.
//
//   2. Perlin, not white. White noise looks like a broken sensor. Perlin
//      drifts smoothly between samples, so it reads as a tremor: an unsteady
//      hand, which is the metaphor the whole project is built on.
//
//   3. In the paper's plane, not all three axes. The original also displaced
//      the pen perpendicular to the page, pushing the tip into and out of the
//      paper surface. Handwriting does not wobble that way.

using UnityEngine;

namespace MXFocus
{
    public static class PenWobble
    {
        /// <summary>How fast the tremor evolves. Higher is more jittery.</summary>
        public const float TremorHz = 6f;

        // Perlin noise is deterministic and mirror-symmetric about the origin,
        // so sampling two axes near (t, 0) would give visibly correlated
        // motion -- a diagonal shake rather than a wander. Offsetting the
        // second lane decorrelates them.
        private const float SecondLaneOffset = 137.3f;

        /// <summary>
        /// Displacement to add to the pen tip this frame.
        ///
        /// focus     : 0-1 from FocusClient. 1 is fully focused, so no wobble.
        /// paper     : the surface, used to keep the offset in its plane.
        ///             Null falls back to world XZ.
        /// amplitude : metres of displacement at total loss of focus.
        /// </summary>
        public static Vector3 Offset(float focus, Transform paper,
                                     float amplitude)
        {
            float severity = 1f - Mathf.Clamp01(focus);
            if (severity <= 0f || amplitude <= 0f)
            {
                return Vector3.zero;
            }

            float t = Time.time * TremorHz;

            // PerlinNoise returns 0-1; recentre to -1..1.
            float x = (Mathf.PerlinNoise(t, 0f) - 0.5f) * 2f;
            float z = (Mathf.PerlinNoise(SecondLaneOffset, t) - 0.5f) * 2f;

            Vector3 local = new Vector3(x, 0f, z) * (severity * amplitude);

            return paper != null ? paper.TransformDirection(local) : local;
        }
    }
}
