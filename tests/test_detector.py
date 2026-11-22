import numpy as np

from motionwatch.detector import MotionDetector, encode_jpeg


def frame(box=None):
    f = np.full((480, 640, 3), 60, dtype=np.uint8)
    if box:
        x, y, w, h = box
        f[y:y + h, x:x + w] = 255
    return f


def test_static_scene_has_no_motion():
    d = MotionDetector(warmup_frames=3)
    for _ in range(10):
        assert d.process(frame()) == []


def test_moving_object_detected():
    d = MotionDetector(warmup_frames=3, min_area=500)
    for _ in range(5):
        d.process(frame())
    boxes = d.process(frame((200, 150, 120, 120)))
    assert boxes, "expected motion"
    x, y, w, h = boxes[0]
    assert 150 <= x <= 220 and 100 <= y <= 170


def test_ignore_region_suppresses_motion():
    d = MotionDetector(warmup_frames=3, min_area=500, ignore_regions=[(0.0, 0.0, 1.0, 0.5)])
    for _ in range(5):
        d.process(frame())
    assert d.process(frame((200, 20, 120, 120))) == []


def test_encode_jpeg():
    assert encode_jpeg(frame())[:2] == b"\xff\xd8"
