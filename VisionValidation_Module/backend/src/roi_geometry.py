"""Validated convex quadrilateral ROI geometry (pixel coordinates in desktop configs)."""
import math
import cv2
import numpy as np


def validate_vertices(vertices):
    points = np.asarray(vertices, dtype=np.float32)
    if points.shape != (4, 2) or not np.isfinite(points).all():
        raise ValueError("ROI needs four finite [x, y] vertices")
    crosses = []
    for i in range(4):
        a, b, c = points[i], points[(i + 1) % 4], points[(i + 2) % 4]
        crosses.append(float((b[0]-a[0])*(c[1]-b[1])-(b[1]-a[1])*(c[0]-b[0])))
    if not (all(v > 1e-6 for v in crosses) or all(v < -1e-6 for v in crosses)):
        raise ValueError("ROI corners must form a non-crossing convex quadrilateral in perimeter order")
    return points


def roi_vertices(roi):
    if "vertices" in roi:
        return validate_vertices(roi["vertices"])
    return validate_vertices([[roi["x_min"], roi["y_min"]], [roi["x_max"], roi["y_min"]],
                              [roi["x_max"], roi["y_max"]], [roi["x_min"], roi["y_max"]]])


def contains_point(roi, point):
    if not all(math.isfinite(float(v)) for v in point):
        return False
    try:
        return cv2.pointPolygonTest(roi_vertices(roi), (float(point[0]), float(point[1])), False) >= 0
    except (ValueError, TypeError, KeyError):
        return False
