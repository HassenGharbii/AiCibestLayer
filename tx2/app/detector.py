"""YOLOX-Nano detector for the TX2's Pascal GPU (256 CUDA cores — far too
weak for YOLOX-s). Deliberately avoids yolox.data / yolox.utils.postprocess
(which need torchvision, pycocotools, tqdm, tensorboard, scikit-image) —
preprocessing and NMS are reimplemented here in plain numpy/cv2 instead.
"""

import urllib.request
from pathlib import Path
from typing import Tuple

import cv2
import numpy as np

from .config import WEIGHTS_DIR

MODEL_NAME = "yolox-nano"
WEIGHTS_URL = "https://github.com/Megvii-BaseDetection/YOLOX/releases/download/0.1.1rc0/yolox_nano.pth"
WEIGHTS_PATH = WEIGHTS_DIR / "yolox_nano.pth"
TEST_SIZE = (416, 416)  # yolox-nano's native input size

CONFIDENCE_THRESHOLD = 0.25
NMS_THRESHOLD = 0.45

_EMPTY_XYXY = np.empty((0, 4), dtype=np.float32)
_EMPTY_1D = np.empty((0,), dtype=np.float32)
_EMPTY_CLASS_ID = np.empty((0,), dtype=int)


def _ensure_weights() -> Path:
    WEIGHTS_DIR.mkdir(parents=True, exist_ok=True)
    if not WEIGHTS_PATH.exists():
        urllib.request.urlretrieve(WEIGHTS_URL, WEIGHTS_PATH)
    return WEIGHTS_PATH


def _preprocess(frame: np.ndarray, input_size: Tuple[int, int]) -> Tuple[np.ndarray, float]:
    """Letterbox resize + CHW float32, matching yolox.data.data_augment.preproc."""
    padded = np.full((input_size[0], input_size[1], 3), 114, dtype=np.uint8)
    ratio = min(input_size[0] / frame.shape[0], input_size[1] / frame.shape[1])
    resized = cv2.resize(
        frame,
        (int(frame.shape[1] * ratio), int(frame.shape[0] * ratio)),
        interpolation=cv2.INTER_LINEAR,
    )
    padded[: resized.shape[0], : resized.shape[1]] = resized
    padded = padded.transpose(2, 0, 1)  # HWC -> CHW
    return np.ascontiguousarray(padded, dtype=np.float32), ratio


def _nms(boxes: np.ndarray, scores: np.ndarray, iou_threshold: float) -> np.ndarray:
    """Greedy NMS, class-agnostic — no torchvision.ops needed."""
    x1, y1, x2, y2 = boxes[:, 0], boxes[:, 1], boxes[:, 2], boxes[:, 3]
    areas = (x2 - x1) * (y2 - y1)
    order = scores.argsort()[::-1]
    keep = []
    while order.size > 0:
        i = order[0]
        keep.append(i)
        xx1 = np.maximum(x1[i], x1[order[1:]])
        yy1 = np.maximum(y1[i], y1[order[1:]])
        xx2 = np.minimum(x2[i], x2[order[1:]])
        yy2 = np.minimum(y2[i], y2[order[1:]])
        inter = np.maximum(0.0, xx2 - xx1) * np.maximum(0.0, yy2 - yy1)
        iou = inter / (areas[i] + areas[order[1:]] - inter)
        order = order[1:][iou <= iou_threshold]
    return np.array(keep, dtype=int)


class Detector:
    def __init__(self):
        import torch
        from yolox.exp import get_exp

        self._torch = torch
        self.exp = get_exp(exp_name=MODEL_NAME)
        self.model = self.exp.get_model()
        checkpoint = torch.load(_ensure_weights(), map_location="cpu")
        self.model.load_state_dict(checkpoint["model"])
        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        # FP16 roughly halves inference time on Pascal-class GPUs (this board's
        # GPU is weak enough that this matters a lot); not worth it on CPU.
        self.use_half = self.device == "cuda"
        self.model.to(self.device)
        if self.use_half:
            self.model.half()
        self.model.eval()

    def detect(self, frame: np.ndarray) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
        """Returns (xyxy, confidence, class_id) in the frame's own pixel space."""
        img, ratio = _preprocess(frame, TEST_SIZE)
        tensor = self._torch.from_numpy(img).unsqueeze(0).to(self.device)
        tensor = tensor.half() if self.use_half else tensor.float()
        with self._torch.no_grad():
            outputs = self.model(tensor)

        # outputs: (1, N, 5 + num_classes) = (cx, cy, w, h, obj_conf, class_confs...)
        prediction = outputs[0].float().cpu().numpy()
        boxes_cxcywh = prediction[:, :4]
        obj_conf = prediction[:, 4]
        class_confs = prediction[:, 5:]
        class_id = class_confs.argmax(axis=1)
        class_conf = class_confs[np.arange(len(class_confs)), class_id]
        confidence = obj_conf * class_conf

        keep_conf = confidence >= CONFIDENCE_THRESHOLD
        if not np.any(keep_conf):
            return _EMPTY_XYXY, _EMPTY_1D, _EMPTY_CLASS_ID

        boxes_cxcywh = boxes_cxcywh[keep_conf]
        confidence = confidence[keep_conf]
        class_id = class_id[keep_conf]

        xyxy = np.empty_like(boxes_cxcywh)
        xyxy[:, 0] = boxes_cxcywh[:, 0] - boxes_cxcywh[:, 2] / 2
        xyxy[:, 1] = boxes_cxcywh[:, 1] - boxes_cxcywh[:, 3] / 2
        xyxy[:, 2] = boxes_cxcywh[:, 0] + boxes_cxcywh[:, 2] / 2
        xyxy[:, 3] = boxes_cxcywh[:, 1] + boxes_cxcywh[:, 3] / 2

        keep_nms = _nms(xyxy, confidence, NMS_THRESHOLD)
        xyxy, confidence, class_id = xyxy[keep_nms], confidence[keep_nms], class_id[keep_nms]

        xyxy /= ratio
        return xyxy.astype(np.float32), confidence.astype(np.float32), class_id.astype(int)
