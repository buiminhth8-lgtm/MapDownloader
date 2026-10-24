"""单区域瓦片估算：后台计算、候选瓦片级取消，只通过信号返回结果。"""

from __future__ import annotations

import threading

from PySide6.QtCore import QThread, Signal

from app.geo.region_estimator import RegionEstimateCancelled, RegionTileEstimator


class RegionEstimateWorker(QThread):
    estimated = Signal(object)
    progressed = Signal(object)
    failed = Signal(str)

    def __init__(self, region, min_zoom, max_zoom, parent=None, *, geometry=None):
        super().__init__(parent)
        self._region = region
        self._min_zoom = min_zoom
        self._max_zoom = max_zoom
        self._geometry = geometry
        self._stop = threading.Event()

    @property
    def cancel_requested(self) -> bool:
        return self._stop.is_set()

    def cancel(self) -> None:
        self._stop.set()

    def run(self) -> None:
        try:
            result = RegionTileEstimator(self._region, geometry=self._geometry).estimate(
                self._min_zoom,
                self._max_zoom,
                should_cancel=self._stop.is_set,
                progress=lambda zoom, done, total: self.progressed.emit((zoom, done, total)),
            )
            if not self.cancel_requested:
                self.estimated.emit(result)
        except RegionEstimateCancelled:
            pass
        except Exception as exc:
            self.failed.emit(f"估算失败：{exc}")
