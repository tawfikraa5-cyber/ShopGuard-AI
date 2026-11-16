import unittest
from vision import EvidenceTracker, SceneGuard, hand_cues


class EvidenceTests(unittest.TestCase):
    zone = [0.2, 0.2, 0.8, 0.8]

    def detection(self, confidence=0.9, inside=True, hands=None):
        return {
            "id": 1,
            "box": [0.3, 0.2, 0.5, 0.7] if inside else [0.85, 0.2, 0.98, 0.7],
            "confidence": confidence,
            "hands": hands or {},
        }

    def test_sampled_dwell_uses_video_time_and_preserves_peak(self):
        tracker = EvidenceTracker(2)
        alerts = []
        for i in range(13):
            alerts.extend(
                v for v in tracker.update([self.detection()], i / 6, self.zone) if v["ai"]
            )
        last = alerts[-1]
        self.assertTrue(last["ai"])
        self.assertEqual(last["reasons"], ["dwell"])
        after = tracker.update([self.detection(inside=False)], 2.2, self.zone)[0]
        self.assertEqual(after["duration"], 2)

    def test_missing_detections_do_not_add_unobserved_time(self):
        tracker = EvidenceTracker(1)
        tracker.update([self.detection()], 0, self.zone)
        tracker.update([], 0.5, self.zone)
        last = tracker.update([self.detection()], 1.2, self.zone)[0]
        self.assertFalse(last["ai"])
        self.assertEqual(last["duration"], 0)

    def test_low_confidence_and_short_tracks_do_not_alert(self):
        tracker = EvidenceTracker(1)
        for i in range(13):
            last = tracker.update([self.detection(0.15)], i / 6, self.zone)[0]
        self.assertFalse(last["ai"])

    def test_pose_requires_repeated_extension_return_cycles(self):
        tracker = EvidenceTracker(100)
        alerts = []
        for i, cue in enumerate(["extended", "extended", "torso", "extended", "extended", "torso"]):
            alerts.extend(
                v
                for v in tracker.update([self.detection(hands={9: cue})], i * 0.5, self.zone)
                if v["ai"]
            )
        last = alerts[-1]
        self.assertTrue(last["ai"])
        self.assertIn("hand_return", last["reasons"])
        self.assertEqual(last["evidence"]["hand_cycles"], 2)

    def test_scene_reset_prevents_linking_people_between_shots(self):
        tracker = EvidenceTracker(2)
        first = tracker.update([self.detection()], 0, self.zone)[0]
        tracker.reset()
        last = tracker.update([self.detection()], 10, self.zone)[0]
        self.assertNotEqual(first["id"], last["id"])
        self.assertFalse(last["ai"])

    def test_unreliable_keypoints_cannot_trigger_hand_cues(self):
        self.assertEqual(hand_cues([[0, 0, 0.1]] * 17, [0, 0, 100, 100], 100), {})

    def test_three_real_entries_with_observed_time_trigger_reentry(self):
        tracker = EvidenceTracker(100)
        second = 0
        alerts = []
        for visit in range(3):
            for i in range(8):
                alerts.extend(
                    v for v in tracker.update([self.detection()], second, self.zone) if v["ai"]
                )
                second += 0.2
            tracker.update([self.detection(inside=False)], second, self.zone)
            second += 0.2
        self.assertTrue(any("reentry" in v["reasons"] for v in alerts))

    def test_later_hand_signal_is_not_hidden_by_dwell_and_evidence_is_frozen(self):
        import json

        tracker = EvidenceTracker(1)
        alerts = []
        for i in range(5):
            alerts.extend(
                v for v in tracker.update([self.detection()], i * 0.25, self.zone) if v["ai"]
            )
        dwell = next(v for v in alerts if v["reasons"] == ["dwell"])
        frozen = json.dumps(dwell, sort_keys=True)
        for second, cue in [
            (1.25, "extended"),
            (1.5, "extended"),
            (1.75, "torso"),
            (2.0, "extended"),
            (2.25, "extended"),
            (2.5, "extended"),
            (2.75, "torso"),
        ]:
            alerts.extend(
                v
                for v in tracker.update([self.detection(hands={9: cue})], second, self.zone)
                if v["ai"]
            )
        hand = next(v for v in alerts if v["reasons"] == ["hand_return"])
        self.assertNotEqual(dwell["id"], hand["id"])
        self.assertGreater(hand["evidence"]["trigger_second"], dwell["evidence"]["trigger_second"])
        self.assertEqual(frozen, json.dumps(dwell, sort_keys=True))
        for i in range(12, 20):
            self.assertFalse(
                any(v["ai"] for v in tracker.update([self.detection()], i * 0.25, self.zone))
            )

    def test_large_scene_cut_is_detected_but_small_local_motion_is_not(self):
        import numpy as np

        guard = SceneGuard()
        a = np.zeros((120, 160, 3), dtype=np.uint8)
        a[:, :, 2] = 240
        self.assertFalse(guard.update(a))
        local = a.copy()
        local[5:15, 5:15] = 255
        self.assertFalse(guard.update(local))
        b = np.zeros_like(a)
        b[:, :, 0] = 240
        self.assertTrue(guard.update(b))

    def test_live_sample_interval_uses_observed_elapsed_time(self):
        tracker = EvidenceTracker(1, sample_interval=0.5)
        alerts = []
        for second in (0, 0.55, 1.1):
            alerts.extend(
                v for v in tracker.update([self.detection()], second, self.zone) if v["ai"]
            )
        self.assertEqual(len(alerts), 1)
        self.assertAlmostEqual(alerts[0]["duration"], 1.1)


if __name__ == "__main__":
    unittest.main()
