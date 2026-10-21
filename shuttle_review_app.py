#!/usr/bin/env python3
"""Shuttle review app: live stream / video -> YOLO (best.pt) -> trajectory -> 3D review.

  1. Reads frames from a webcam index, a video file or a stream URL (RTSP/HTTP).
  2. Detects the shuttlecock in every frame with the YOLO model (best.pt).
  3. Tracks it, notices when it falls and comes to rest, and saves every fall
     (image track + court co-ordinates) to the session folder.
  4. "Review" freezes the video and replays the saved fall on the 3D court of
     tripods.html (or index.html, --page index) with shuttle.js, in the browser.
     The app serves this folder on http://localhost:8000 itself; keep that tab
     open and every Review replays there (?review=live).

Court co-ordinates are the same as camera_link.py / tripods.html:
  1 unit = 1 m, X = width, Y = height, Z = length, net centre = (0, 0, 0).

One camera sees a ray, not a depth.  The landing spot is exact (the pixel ray
meets the floor).  The height of the earlier points is estimated by assuming
the shuttle falls in a vertical plane through the landing spot
(--plane view: facing the camera, --plane length: along the court length).
Use "Calibrate" so the pixels map onto the real court.

Run
  python3 shuttle_review_app.py --source 0                    # webcam 0
  python3 shuttle_review_app.py --source match.mp4 --cam 2
  python3 shuttle_review_app.py --source rtsp://... --model best.pt
Keys: Space = Review / Resume, Esc = cancel calibration.
"""
import argparse
import csv
import json
import math
import queue
import threading
import time
import webbrowser
from dataclasses import dataclass
from datetime import datetime
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

import cv2
import numpy as np
from PIL import Image, ImageTk

import camera_link as cl

HERE = Path(__file__).resolve().parent
MODEL_CANDIDATES = [HERE / 'best.pt', HERE.parent / 'sample_folder' / 'best.pt']
CORNERS = [(-1, -1), (1, -1), (1, 1), (-1, 1)]     # Cam 1..4, as in camera_link.py
HW, HL = cl.HALF_W, cl.HALF_L
LIVE_PREFIXES = ('rtsp://', 'rtmp://', 'http://', 'https://', 'udp://', 'tcp://')

# Calibration: the part of the court a camera sees (-1 / +1 = that half, 0 = full court).
COURT_PARTS = {'-Z half (Cam 1, 2 side)': -1, '+Z half (Cam 3, 4 side)': 1, 'Full court': 0}
CAM_PART = {1: -1, 2: -1, 3: 1, 4: 1}
MAP_W, MAP_H = 170, 200          # calibration mini map, px


def landmarks(part):
    """Floor points to click when calibrating: [(name, (x, z))]. Each half has 15 line
    crossings + the 2 net post feet. The outer, well spread crossings come first, so
    4 clicks already give a usable camera (and the hint ring for the next point)."""
    sw = cl.SINGLES_WIDTH / 2
    rows = {'back': ('back line', HL), 'long': ('long service line', HL - 0.76), 'short': ('short service line', 1.98)}
    cols = {'-D': ('-X doubles sideline', -HW), '+D': ('+X doubles sideline', HW), 'C': ('centre line', 0.0),
            '-S': ('-X singles sideline', -sw), '+S': ('+X singles sideline', sw)}
    order = [('back', '-D'), ('back', '+D'), ('short', '-D'), ('short', '+D'), ('long', '-D'), ('long', '+D'),
             'post', ('back', 'C'), ('long', 'C'), ('short', 'C'),
             ('back', '-S'), ('back', '+S'), ('long', '-S'), ('long', '+S'), ('short', '-S'), ('short', '+S')]
    out = []
    for s in ([part] if part else [-1, 1]):
        for key in order:
            if key == 'post':
                if s == (part or -1):         # net posts once, even for the full court
                    out += [('net post foot -X (on the floor)', (-HW, 0.0)), ('net post foot +X (on the floor)', (HW, 0.0))]
                continue
            (rname, z), (cname, x) = rows[key[0]], cols[key[1]]
            corner = ' (court corner)' if key == ('back', '-D') or key == ('back', '+D') else ''
            out.append((f"{rname} {'-Z' if s < 0 else '+Z'} x {cname}{corner}", (x, s * z)))
    return out


# ---- Detections and tracking -------------------------------------------------
@dataclass
class Det:
    frame: int
    t: float        # seconds from the start of the source
    u: float        # bbox centre, px
    v: float
    foot: float     # bbox bottom, px (floor contact when the shuttle is on the court)
    w: float
    h: float
    conf: float


@dataclass
class Fall:
    points: list            # Det list: visible flight -> floor contact -> rest
    contact: int            # index of the first floor contact in `points`
    id: int = 0
    estimated: bool = False  # built by Review from the live track, no landing detected
    image: str = ''          # saved snapshot


class Track:
    def __init__(self, tid, d):
        self.id = tid
        self.dets = [d]
        self.armed = True        # may report a fall
        self.rest = None         # (u, v) where its last fall came to rest
        self.after = -1          # last frame of its previous fall (never reused)


class ShuttleTracker:
    """Follows every detected shuttle and reports each fall.

    Detections are linked frame to frame by nearest neighbour into tracks.
    A fall = a track that moved and then stayed (almost) still for `still_n`
    frames (0.5 s, longer than the pause at the top of a bounce).  Tracks that
    never move (a shuttle lying on the floor, a false positive) never report a fall.
    """

    def __init__(self, width, height, fps):
        self.gate = 0.12 * width               # max jump per frame between detections
        self.still_px = max(3.0, 0.006 * width)
        self.still_n = max(5, round(fps * 0.5))
        self.min_move = 0.05 * height          # travel needed before a rest counts as a fall
        self.fall_secs = 1.5                   # trajectory kept before the rest
        self.max_gap = 0.4                     # s without a detection ends a track
        self.tracks = []
        self.next_id = 1
        self.lock = threading.Lock()

    def update(self, frame, t, dets):
        """Feed one frame's detections. Returns ([(track id, Det)], [Fall])."""
        with self.lock:
            self.tracks = [tr for tr in self.tracks if t - tr.dets[-1].t <= self.max_gap]
            pairs = sorted((math.hypot(d.u - tr.dets[-1].u, d.v - tr.dets[-1].v), i, j)
                           for i, tr in enumerate(self.tracks) for j, d in enumerate(dets))
            used_t, used_d, linked, falls = set(), set(), [], []
            for dist, i, j in pairs:
                tr = self.tracks[i]
                if i in used_t or j in used_d or dist > self.gate * (frame - tr.dets[-1].frame):
                    continue
                used_t.add(i)
                used_d.add(j)
                self._append(tr, dets[j], falls)
                linked.append((tr.id, dets[j]))
            for j, d in enumerate(dets):
                if j not in used_d:
                    tr = Track(self.next_id, d)
                    self.next_id += 1
                    self.tracks.append(tr)
                    linked.append((tr.id, d))
            return linked, falls

    def flush(self):
        """End of the video: report falls whose rest was cut short (>= 5 still frames)."""
        with self.lock:
            falls = []
            for tr in self.tracks:
                fall = self._landing([d for d in tr.dets if d.frame > tr.after], 5) if tr.armed else None
                if fall:
                    tr.armed = False
                    falls.append(fall)
            return falls

    def tail(self, secs=1.0):
        """Recent image points of the most active track (for the live overlay)."""
        with self.lock:
            tr = self._active()
            return [(d.u, d.v) for d in tr.dets if d.t >= tr.dets[-1].t - secs] if tr else []

    def current_fall(self):
        """The most active live track as a fall (for Review when no landing was detected)."""
        with self.lock:
            tr = self._active()
            pts = list(tr.dets) if tr else []
        if len(pts) < 2:
            return None
        pts = [d for d in pts if d.t >= pts[-1].t - self.fall_secs]
        return Fall(points=pts, contact=len(pts) - 1, estimated=True)

    def _active(self):
        best, score = None, -1.0
        for tr in self.tracks:
            last = tr.dets[-1]
            s = max(math.hypot(d.u - last.u, d.v - last.v) for d in tr.dets if d.t >= last.t - 1.0)
            if s > score:
                best, score = tr, s
        return best

    def _append(self, tr, d, falls):
        tr.dets.append(d)
        while tr.dets[0].t < d.t - self.fall_secs - 2.0:
            tr.dets.pop(0)
        if not tr.armed and math.hypot(d.u - tr.rest[0], d.v - tr.rest[1]) > self.min_move:
            tr.armed = True              # picked up or hit again (a small slide is not enough)
        if tr.armed:
            fall = self._landing([q for q in tr.dets if q.frame > tr.after], self.still_n)
            if fall:
                tr.armed, tr.rest, tr.after = False, (d.u, d.v), d.frame
                falls.append(fall)

    def _landing(self, dets, still_n):
        if len(dets) < still_n + 2:
            return None
        tail = dets[-still_n:]
        if tail[-1].frame - tail[0].frame > 2 * still_n:
            return None
        mu = sum(d.u for d in tail) / len(tail)
        mv = sum(d.v for d in tail) / len(tail)
        if any(math.hypot(d.u - mu, d.v - mv) > self.still_px for d in tail):
            return None
        i_still = len(dets) - still_n
        moving = dets[:i_still]
        if not moving or max(math.hypot(d.u - mu, d.v - mv) for d in moving) < self.min_move:
            return None
        k = self._contact(dets, i_still)
        start = k                        # keep `fall_secs` of flight before the first contact
        while start > 0 and dets[start - 1].t >= dets[k].t - self.fall_secs:
            start -= 1
        return Fall(points=list(dets[start:]), contact=k - start)

    def _contact(self, seg, i_still):
        """Index of the first floor contact: the lowest image point where the fast
        downward motion stops (bounces and slides come after it)."""
        speed = [(seg[i + 1].v - seg[i].v) / max(1, seg[i + 1].frame - seg[i].frame) for i in range(i_still)]
        if not speed:
            return i_still
        peak = max(range(len(speed)), key=speed.__getitem__)
        if speed[peak] <= self.still_px:
            return i_still               # no clear downward motion: first still frame
        for i in range(peak + 1, len(speed) + 1):
            if i == len(speed) or speed[i] < 0.3 * speed[peak]:
                # the slowed step may still end lower (impact between frames), not a slide/jitter
                j = i
                while j < min(i + 2, i_still) and seg[j + 1].v - seg[j].v > self.still_px:
                    j += 1
                return j
        return i_still


# ---- Camera geometry -----------------------------------------------------------
class CourtCamera:
    """Pinhole camera in court co-ordinates (no lens distortion)."""

    def __init__(self, K, R, pos, size, label):
        self.K, self.Kinv = K, np.linalg.inv(K)
        self.R = R                    # camera -> world rotation (OpenCV axes: x right, y down, z forward)
        self.pos = np.asarray(pos, float)
        self.size = size
        self.label = label

    @classmethod
    def from_tripod(cls, cam_no, w, h, a):
        """The tripod rig of tripods.html / camera_link.py."""
        sx, sz = CORNERS[cam_no - 1]
        c = cl.Camera(cam_no, sx, sz, a.distance, a.height, a.angle, a.tilt, a.hfov, w, h)
        K = np.array([[c.f, 0, w / 2], [0, c.f, h / 2], [0, 0, 1.0]])
        R = np.column_stack([c.right, [-x for x in c.up], c.forward])
        return cls(K, R, c.pos, (w, h), f'Cam {cam_no}: tripod model (not calibrated)')

    @classmethod
    def from_points(cls, cam_no, w, h, hfov, pixels, floor_xz):
        """Camera pose from >= 4 clicked floor landmarks (focal length from the lens FOV)."""
        f = (w / 2) / math.tan(math.radians(hfov) / 2)
        K = np.array([[f, 0, w / 2], [0, f, h / 2], [0, 0, 1.0]])
        obj = np.array([(x, cl.FLOOR_Y, z) for x, z in floor_xz], np.float64)
        img = np.array(pixels, np.float64)
        # A flat target has two mirror-image solutions: keep the best one above the floor.
        n, rvecs, tvecs, _ = cv2.solvePnPGeneric(obj, img, K, None, flags=cv2.SOLVEPNP_IPPE)
        best = None
        for rvec, tvec in zip(rvecs[:n], tvecs[:n]):
            rvec, tvec = cv2.solvePnPRefineLM(obj, img, K, None, rvec.copy(), tvec.copy())
            Rcw, _ = cv2.Rodrigues(rvec)
            pos = (-Rcw.T @ tvec).ravel()
            if pos[1] <= cl.FLOOR_Y:
                continue
            proj, _ = cv2.projectPoints(obj, rvec, tvec, K, None)
            err = float(np.sqrt(np.mean(np.sum((proj.reshape(-1, 2) - img) ** 2, axis=1))))
            if best is None or err < best[0]:
                best = (err, Rcw, pos)
        if best is None:
            raise ValueError('camera came out below the floor - check the clicked points')
        err, Rcw, pos = best
        cam = cls(K, Rcw.T, pos, (w, h),
                  f'Cam {cam_no}: calibrated, {len(obj)} points, {hfov:.0f}° lens, {err:.1f} px error')
        cam.error, cam.hfov = err, hfov
        return cam

    @classmethod
    def fit(cls, cam_no, w, h, hfov, pixels, floor_xz):
        """Like from_points; with >= 6 points the lens FOV is also estimated
        (the one with the smallest click error), otherwise `hfov` is used."""
        if len(pixels) < 6:
            return cls.from_points(cam_no, w, h, hfov, pixels, floor_xz)
        best = None
        for step, lo, hi in ((2.0, 30.0, 120.0), (0.2, None, None)):
            if lo is None:
                lo, hi = best.hfov - 2, best.hfov + 2
            for f in np.arange(lo, hi + 1e-6, step):
                try:
                    cam = cls.from_points(cam_no, w, h, float(f), pixels, floor_xz)
                except (ValueError, cv2.error):
                    continue
                if best is None or cam.error < best.error:
                    best = cam
            if best is None:
                raise ValueError('no lens FOV fits these points - check the clicks')
        return best

    def ray(self, u, v):
        d = self.R @ (self.Kinv @ np.array([u, v, 1.0]))
        return d / np.linalg.norm(d)

    def floor_hit(self, u, v):
        d = self.ray(u, v)
        if d[1] >= -1e-9:
            return None              # above the horizon
        return self.pos + d * ((cl.FLOOR_Y - self.pos[1]) / d[1])

    def project(self, p):
        pc = self.R.T @ (np.asarray(p, float) - self.pos)
        if pc[2] < 0.05:
            return None
        uv = self.K @ pc
        return uv[0] / uv[2], uv[1] / uv[2]


def reconstruct(cam, fall, plane='view'):
    """3D points of a fall. Floor points are exact; flight (and bounce) points lie
    in a vertical plane through the landing spot. Returns a list of dicts, or
    None if the contact pixel is not on the floor."""
    c = fall.points[fall.contact]
    land = cam.floor_hit(c.u, c.foot)
    if land is None:
        return None
    if plane == 'length':
        n = np.array([1.0, 0.0, 0.0])
    else:
        n = land - cam.pos
        n[1] = 0.0
        n /= np.linalg.norm(n)
    def in_plane(d):
        ray = cam.ray(d.u, d.v)
        den = ray @ n
        s = ((land - cam.pos) @ n) / den if abs(den) > 1e-6 else -1
        if s <= 0:
            return None
        p = cam.pos + s * ray
        p[1] = max(p[1], cl.FLOOR_Y)
        return p

    # After the contact a point is in the air (a bounce) if the shuttle comes
    # clearly lower in the image again later; otherwise it is on the floor.
    low_after, low = [0.0] * len(fall.points), -math.inf
    for i in range(len(fall.points) - 1, -1, -1):
        low_after[i] = low
        low = max(low, fall.points[i].v)
    bounce_px = 0.025 * cam.size[1]
    out = []
    for i, d in enumerate(fall.points):
        if i < fall.contact:
            p, phase = in_plane(d), 'flight'
        elif i > fall.contact and low_after[i] - d.v > bounce_px:
            p, phase = in_plane(d), 'bounce'
        else:
            p, phase = cam.floor_hit(d.u, d.foot), 'contact' if i == fall.contact else 'floor'
        if p is None:
            continue
        out.append({'frame': d.frame, 't': round(d.t, 4), 'u': round(d.u, 1), 'v': round(d.v, 1),
                    'conf': round(d.conf, 3), 'phase': phase,
                    'x': round(float(p[0]), 3), 'y': round(float(p[1]), 3), 'z': round(float(p[2]), 3)})
    return out


def landing_of(pts3d):
    return next((p for p in pts3d if p['phase'] == 'contact'), pts3d[-1]) if pts3d else None


# ---- Drawing on video frames ---------------------------------------------------
def draw_court_2d(img, cam):
    """Court lines and net tape projected through the camera (checks the calibration)."""
    def poly(points3d, color):
        seg = []
        for p in points3d:
            uv = cam.project(p)
            if uv is None or not (-5000 < uv[0] < 5000 and -5000 < uv[1] < 5000):
                if len(seg) > 1:
                    cv2.polylines(img, [np.int32(seg)], False, color, 1, cv2.LINE_AA)
                seg = []
            else:
                seg.append(uv)
        if len(seg) > 1:
            cv2.polylines(img, [np.int32(seg)], False, color, 1, cv2.LINE_AA)
    for _, (x1, z1), (x2, z2) in cl.COURT_LINES:
        poly([(x1 + (x2 - x1) * k, cl.FLOOR_Y, z1 + (z2 - z1) * k) for k in np.linspace(0, 1, 30)], (255, 255, 0))
    poly([(x, cl.net_height(x), 0.0) for x in np.linspace(-HW, HW, 30)], (255, 255, 255))


def draw_fall_2d(img, fall, color=(0, 165, 255)):
    pts = np.int32([(d.u, d.v) for d in fall.points])
    cv2.polylines(img, [pts], False, color, 2, cv2.LINE_AA)
    for p in pts:
        cv2.circle(img, tuple(int(a) for a in p), 3, color, -1, cv2.LINE_AA)
    c = fall.points[fall.contact]
    cv2.circle(img, (int(c.u), int(c.foot)), 12, (0, 0, 255), 2, cv2.LINE_AA)


# ---- Video worker ----------------------------------------------------------------
def is_live(source):
    return isinstance(source, int) or str(source).lower().startswith(LIVE_PREFIXES)


class VideoWorker(threading.Thread):
    """Reads frames, runs YOLO, tracks the shuttle. Talks to the GUI only through queues.

    Paused: a video file stops; a live stream keeps detecting and saving falls
    (the GUI just stops showing new frames)."""

    def __init__(self, app, source, session):
        super().__init__(daemon=True)
        self.app, self.source, self.session = app, source, session
        self.live = is_live(source)
        self.frames = queue.Queue(maxsize=2)
        self.events = queue.Queue()
        self.paused = threading.Event()
        self.stopped = threading.Event()
        self.tracker = None

    def run(self):
        try:
            self._run()
        except Exception as e:      # report to the GUI instead of dying silently
            self.events.put(('error', f'{type(e).__name__}: {e}'))

    def _run(self):
        model = self.app.get_model(self.events)
        cap = cv2.VideoCapture(self.source)
        if not cap.isOpened():
            self.events.put(('error', f'Cannot open source: {self.source}'))
            return
        if self.live:
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        fps = cap.get(cv2.CAP_PROP_FPS)
        fps = fps if 1 <= fps <= 240 else 30.0
        w, h = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        self.tracker = ShuttleTracker(w, h, fps)
        self.events.put(('info', w, h, fps))
        a = self.app.args
        kw = {'conf': a.conf, 'imgsz': a.imgsz, 'verbose': False}
        if a.device:
            kw['device'] = a.device

        with open(self.session / 'track.csv', 'w', newline='') as fcsv:
            out = csv.writer(fcsv)
            out.writerow(['frame', 'time_s', 'track', 'u', 'v', 'foot_v', 'w', 'h', 'conf'])
            idx, t0, next_t, fails, last_frame = 0, time.monotonic(), time.monotonic(), 0, None
            while not self.stopped.is_set():
                if self.paused.is_set() and not self.live:
                    time.sleep(0.03)
                    next_t = time.monotonic()
                    continue
                ok, frame = cap.read()
                if not ok:
                    fails += 1
                    if self.live and fails < 100:
                        time.sleep(0.05)
                        continue
                    for fall in self.tracker.flush():
                        self.events.put(('fall', fall, last_frame))
                    self.events.put(('eof',))
                    break
                fails, last_frame = 0, frame
                t = time.monotonic() - t0 if self.live else idx / fps
                r = model.predict(frame, **kw)[0]
                dets = [Det(idx, t, (x1 + x2) / 2, (y1 + y2) / 2, y2, x2 - x1, y2 - y1, float(c))
                        for (x1, y1, x2, y2), c in zip(r.boxes.xyxy.cpu().numpy().tolist(),
                                                       r.boxes.conf.cpu().numpy().tolist())]
                linked, falls = self.tracker.update(idx, t, dets)
                for tid, d in linked:
                    out.writerow([d.frame, f'{t:.4f}', tid, f'{d.u:.1f}', f'{d.v:.1f}', f'{d.foot:.1f}',
                                  f'{d.w:.1f}', f'{d.h:.1f}', f'{d.conf:.3f}'])
                for fall in falls:
                    fcsv.flush()
                    self.events.put(('fall', fall, frame.copy()))
                self._put_frame((frame, idx, t, dets, self.tracker.tail()))
                idx += 1
                if not self.live:   # play files at their own speed (slower if YOLO can't keep up)
                    next_t += 1 / fps
                    delay = next_t - time.monotonic()
                    if delay > 0:
                        time.sleep(delay)
                    else:
                        next_t = time.monotonic()
        cap.release()

    def _put_frame(self, item):
        try:
            self.frames.put_nowait(item)
        except queue.Full:
            try:
                self.frames.get_nowait()
            except queue.Empty:
                pass
            self.frames.put_nowait(item)


# ---- 3D review in the browser (index.html / tripods.html + shuttle.js) ------------
class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def end_headers(self):
        self.send_header('Cache-Control', 'no-store')    # always the latest review / page
        super().end_headers()


def start_web_server(port):
    """Serves this folder (the 3D pages + reviews/) on localhost; tries the next ports if busy."""
    for p in range(port, port + 20):
        try:
            srv = ThreadingHTTPServer(('127.0.0.1', p), partial(QuietHandler, directory=str(HERE)))
        except OSError:
            continue
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        return srv, p
    raise OSError(f'no free port in {port}-{port + 19}')


# ---- Main window -----------------------------------------------------------------
class App:
    def __init__(self, root, args):
        self.root, self.args = root, args
        self.model, self.model_lock = None, threading.Lock()
        self.worker = None
        self.frame = None            # last frame shown (raw BGR)
        self.overlay = ([], [])      # (detections, track tail) of that frame
        self.size = None             # (w, h) of the source
        self.camera = None
        self.frozen = False
        self.review_fall = None
        self.falls = []              # (Fall, pts3d)
        self.calib = None            # {'i': landmark index, 'points': [(landmark, u, v)]}
        self.view = (1.0, 0, 0)      # display scale and offset of the frame on the canvas
        self.photo = None
        self.review_seq = 0          # bumps on every Review so the open 3D page replays
        self.browser_opened = False
        self.web, self.web_port = start_web_server(args.port)
        self.build_ui()
        root.protocol('WM_DELETE_WINDOW', self.quit)
        root.bind('<space>', self.on_space)
        root.bind('<Escape>', lambda e: self.calib_cancel())
        root.after(15, self.poll)
        if args.source is not None:
            self.source_var.set(str(args.source))
            root.after(100, self.open_source)

    # -- UI -------------------------------------------------------------------
    def build_ui(self):
        r = self.root
        r.title('Shuttle review')
        r.geometry('1400x820')
        top = ttk.Frame(r, padding=6)
        top.pack(side='top', fill='x')
        ttk.Label(top, text='Source').pack(side='left')
        self.source_var = tk.StringVar(value='0')
        ttk.Entry(top, textvariable=self.source_var, width=42).pack(side='left', padx=4)
        ttk.Button(top, text='Browse…', command=self.browse).pack(side='left')
        ttk.Button(top, text='Open', command=self.open_source).pack(side='left', padx=(4, 14))
        ttk.Label(top, text='Camera').pack(side='left')
        self.cam_var = tk.StringVar(value=str(self.args.cam))
        cam_box = ttk.Combobox(top, textvariable=self.cam_var, values=['1', '2', '3', '4'], width=3, state='readonly')
        cam_box.pack(side='left', padx=4)
        cam_box.bind('<<ComboboxSelected>>', lambda e: self.build_camera())
        ttk.Button(top, text='Calibrate', command=self.calib_start).pack(side='left', padx=(4, 2))
        ttk.Button(top, text='Adjust calibration', command=self.calib_adjust).pack(side='left', padx=(0, 14))
        self.court_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(top, text='Court overlay', variable=self.court_var, command=self.render).pack(side='left')
        ttk.Button(top, text='▶ Resume', command=self.resume).pack(side='right')
        ttk.Button(top, text='⏸ Review', command=self.review).pack(side='right', padx=4)
        ttk.Button(top, text='Open 3D view', command=self.open_3d).pack(side='right', padx=(14, 0))

        # Calibration bar: mini court map (where to click) | part + instructions | buttons.
        self.calib_bar = ttk.Frame(r, padding=6)
        self.calib_map = tk.Canvas(self.calib_bar, width=MAP_W, height=MAP_H, bg='#0c5762', highlightthickness=0)
        self.calib_map.pack(side='left', padx=(0, 10))
        mid = ttk.Frame(self.calib_bar)
        mid.pack(side='left', fill='both', expand=True)
        row = ttk.Frame(mid)
        row.pack(anchor='w')
        ttk.Label(row, text='Court part this camera sees').pack(side='left')
        self.part_var = tk.StringVar()
        part_box = ttk.Combobox(row, textvariable=self.part_var, values=list(COURT_PARTS), width=24, state='readonly')
        part_box.pack(side='left', padx=6)
        part_box.bind('<<ComboboxSelected>>', lambda e: self.calib_set_part())
        self.calib_label = ttk.Label(mid, font=('TkDefaultFont', 12, 'bold'), wraplength=760, justify='left')
        self.calib_label.pack(anchor='w', pady=(6, 2))
        ttk.Label(mid, foreground='#555', wraplength=760, justify='left', text=(
            'Click where the two line centres cross on the floor (the magnifier follows the mouse). Skip points '
            'you cannot see; 4 minimum, 6+ spread out also finds the lens FOV. Adjust: drag a point, or select '
            'it and nudge with the arrow keys (Shift = 5 px); right-click deletes it; click a dot on the map to '
            'add / redo that point. Point colours = fit error: green < 3 px, orange < 8, red = wrong crossing. '
            'Map: red = click now, green = done. The yellow ring shows where the current point should be.')
                  ).pack(anchor='w')
        btns = ttk.Frame(self.calib_bar)
        btns.pack(side='right', fill='y')
        for text, cmd in (('Skip point', self.calib_skip), ('Undo', self.calib_undo),
                          ('Save', self.calib_finish), ('Cancel', self.calib_cancel)):
            ttk.Button(btns, text=text, command=cmd, width=11).pack(side='top', pady=1)

        self.status = ttk.Label(r, padding=(8, 3), anchor='w')
        self.status.pack(side='bottom', fill='x')
        body = ttk.Frame(r)
        body.pack(side='top', fill='both', expand=True)
        side = ttk.Frame(body, padding=6, width=300)
        side.pack(side='right', fill='y')
        ttk.Label(side, text='Saved falls (select, then Review)').pack(anchor='w')
        self.listbox = tk.Listbox(side, width=40, font=('TkFixedFont', 10), activestyle='none')
        self.listbox.pack(fill='both', expand=True, pady=4)
        self.listbox.bind('<Double-Button-1>', lambda e: self.review())
        self.cam_label = ttk.Label(side, wraplength=290, justify='left')
        self.cam_label.pack(anchor='w', pady=(6, 0))
        self.session_label = ttk.Label(side, wraplength=290, justify='left', foreground='#555')
        self.session_label.pack(anchor='w', pady=(6, 0))
        self.canvas = tk.Canvas(body, bg='black', highlightthickness=0)
        self.canvas.pack(side='left', fill='both', expand=True)
        self.canvas.bind('<Button-1>', self.on_press)
        self.canvas.bind('<B1-Motion>', self.on_drag)
        self.canvas.bind('<ButtonRelease-1>', self.on_release)
        self.canvas.bind('<Button-3>', self.on_right_click)
        self.canvas.bind('<Motion>', self.on_motion)
        self.canvas.bind('<Configure>', lambda e: self.render())
        self.calib_map.bind('<Button-1>', self.on_map_click)
        for key, dx, dy in (('Left', -1, 0), ('Right', 1, 0), ('Up', 0, -1), ('Down', 0, 1)):
            r.bind(f'<{key}>', lambda e, dx=dx, dy=dy: self.calib_nudge(e, dx, dy, 1))
            r.bind(f'<Shift-{key}>', lambda e, dx=dx, dy=dy: self.calib_nudge(e, dx, dy, 5))
        r.bind('<Delete>', lambda e: self.calib_delete())
        self.set_status('Choose a source (webcam index, video file or stream URL) and press Open.')

    def on_space(self, ev):
        if isinstance(ev.widget, (tk.Entry, ttk.Entry)):
            return               # typing a source path
        if self.frozen:
            self.resume()
        else:
            self.review()

    def set_status(self, text):
        self.status.config(text=text)

    def browse(self):
        path = filedialog.askopenfilename(title='Video file', filetypes=[
            ('Video', '*.mp4 *.avi *.mov *.mkv *.m4v *.webm'), ('All files', '*')])
        if path:
            self.source_var.set(path)
            self.open_source()

    # -- Model / source ---------------------------------------------------------
    def get_model(self, events):
        """Loads best.pt once (called from the worker thread)."""
        with self.model_lock:
            if self.model is None:
                path = self.args.model or next((p for p in MODEL_CANDIDATES if p.exists()), None)
                if path is None or not Path(path).exists():
                    raise FileNotFoundError(f'model not found: {path or MODEL_CANDIDATES[0]} (use --model)')
                events.put(('status', f'Loading {path} …'))
                from ultralytics import YOLO
                self.model = YOLO(str(path))
                self.model_path = path
            return self.model

    def open_source(self):
        text = self.source_var.get().strip()
        if not text:
            return
        source = int(text) if text.isdigit() else text
        if not is_live(source) and not Path(source).exists():
            messagebox.showerror('Shuttle review', f'File not found:\n{source}')
            return
        self.stop_worker()
        name = 'cam' + text if isinstance(source, int) else Path(text).stem if not is_live(source) else 'stream'
        session = Path(self.args.out) / f"{datetime.now():%Y%m%d_%H%M%S}_{name}"
        session.mkdir(parents=True, exist_ok=True)
        self.session = session
        self.falls.clear()
        self.listbox.delete(0, 'end')
        self.frame, self.frozen, self.review_fall = None, False, None
        self.session_label.config(text=f'Saving to {session}')
        self.worker = VideoWorker(self, source, session)
        self.worker.start()
        self.set_status(f'Opening {text} …')

    def stop_worker(self):
        if self.worker:
            self.worker.stopped.set()
            self.worker.join(timeout=2)
            self.worker = None

    def quit(self):
        self.stop_worker()
        self.web.shutdown()
        self.root.destroy()

    # -- Camera -------------------------------------------------------------------
    def calib_path(self, cam_no):
        return HERE / f'calib_cam{cam_no}.json'

    def build_camera(self):
        if not self.size:
            return
        cam_no = int(self.cam_var.get())
        w, h = self.size
        self.camera = CourtCamera.from_tripod(cam_no, w, h, self.args)
        path = self.calib_path(cam_no)
        if path.exists():
            try:
                c = json.loads(path.read_text())
                if tuple(c['image_size']) == (w, h):
                    cam = CourtCamera.from_points(cam_no, w, h, c['hfov'], [p['pixel'] for p in c['points']],
                                                  [p['world'] for p in c['points']])
                    if cam.error > 8:      # clicks on the wrong crossings: do not trust it
                        self.set_status(f'{path.name} fits badly ({cam.error:.0f} px error) - using the tripod '
                                        f'model. Press Calibrate to redo Cam {cam_no}.')
                    else:
                        self.camera = cam
                else:
                    self.set_status(f'{path.name} is for {c["image_size"]}, video is {w}x{h} - using tripod model')
            except (ValueError, KeyError, json.JSONDecodeError) as e:
                self.set_status(f'Ignoring {path.name}: {e}')
        self.cam_label.config(text=self.camera.label + ('' if 'calibrated' in self.camera.label else
                                                        '\nPress Calibrate and click court landmarks for real positions.'))
        self.render()

    def calib_start(self, points=None, part_name=None):
        """New calibration, or (with `points`) editing a saved one: [(world (x, z), u, v)]."""
        if self.frame is None:
            self.set_status('Open a source first.')
            return
        self.freeze()
        part = CAM_PART[int(self.cam_var.get())]
        self.part_var.set(part_name or next(k for k, v in COURT_PARTS.items() if v == part))
        self.calib = {'points': [], 'saved': points or [], 'sel': None, 'zoom': None, 'drag': False}
        self.calib_bar.pack(side='top', fill='x', after=self.root.winfo_children()[0])
        self.calib_set_part()

    def calib_adjust(self):
        """Loads the saved calibration of this camera so its points can be moved, added or deleted."""
        path = self.calib_path(int(self.cam_var.get()))
        if not path.exists():
            self.set_status(f'No saved calibration for Cam {self.cam_var.get()} yet - press Calibrate first.')
            return
        try:
            c = json.loads(path.read_text())
            pts = [(tuple(p['world']), *p['pixel']) for p in c['points']]
        except (ValueError, KeyError) as e:
            self.set_status(f'Cannot read {path.name}: {e}')
            return
        if self.size and tuple(c.get('image_size', self.size)) != tuple(self.size):
            self.set_status(f'{path.name} was made for {c["image_size"]}, this video is {self.size} - recalibrate.')
            return
        part = c.get('part') if c.get('part') in COURT_PARTS else None
        if part is None:            # older files: the camera's half if all points are on it, else the full court
            half = landmarks(CAM_PART[int(self.cam_var.get())])
            on_half = all(any(np.allclose(w, m[1]) for m in half) for w, _, _ in pts)
            part = next(k for k, v in COURT_PARTS.items() if v == (CAM_PART[int(self.cam_var.get())] if on_half else 0))
        self.calib_start(pts, part)
        self.set_status(f'Adjusting {path.name}: drag points / arrow keys, then Save.')

    def calib_set_part(self):
        """Uses the landmark list of the chosen court part; clicked points on it are kept."""
        c = self.calib
        if c is None:
            return
        keep = c.pop('saved', None) or [(c['marks'][i][1], u, v) for i, u, v in c['points']]
        part = COURT_PARTS[self.part_var.get()]
        marks = landmarks(part)
        points = []
        for world, u, v in keep:
            i = next((k for k, m in enumerate(marks) if np.allclose(m[1], world)), None)
            if i is not None:
                points.append([i, u, v])
        c.update(part=part, marks=marks, points=points, sel=None)
        c['i'] = self.calib_next_free(-1)
        self.calib_prompt()

    def calib_next_free(self, after):
        """First landmark after index `after` that has no point yet."""
        done = {i for i, _, _ in self.calib['points']}
        j = after + 1
        while j < len(self.calib['marks']) and j in done:
            j += 1
        return j

    def calib_fit(self, quick=False):
        """Provisional camera + per-point error from the points so far. `quick` keeps the lens FOV
        (fast enough to run while a point is dragged)."""
        c = self.calib
        c['guess'], c['resid'] = None, {}
        if len(c['points']) < 4:
            return
        pixels = [(u, v) for _, u, v in c['points']]
        world = [c['marks'][i][1] for i, _, _ in c['points']]
        cam_no, (w, h) = int(self.cam_var.get()), self.size
        try:
            if quick and c.get('last_hfov'):
                c['guess'] = CourtCamera.from_points(cam_no, w, h, c['last_hfov'], pixels, world)
            else:
                c['guess'] = CourtCamera.fit(cam_no, w, h, self.args.hfov, pixels, world)
                c['last_hfov'] = c['guess'].hfov
        except (ValueError, cv2.error):
            return
        for i, u, v in c['points']:
            x, z = c['marks'][i][1]
            p = c['guess'].project((x, cl.FLOOR_Y, z))
            c['resid'][i] = math.hypot(p[0] - u, p[1] - v) if p else 999.0

    def calib_prompt(self, quick=False):
        c = self.calib
        self.calib_fit(quick)
        n = len(c['points'])
        fit = f"  ·  fit: {c['guess'].hfov:.0f}° lens, {c['guess'].error:.1f} px" if c['guess'] else ''
        if c['sel'] is not None:
            i = c['points'][c['sel']][0]
            r = c['resid'].get(i)
            text = (f"Selected point {i + 1}: {c['marks'][i][0]}" + (f'  (error {r:.1f} px)' if r is not None else '') +
                    f'  -  drag it or use the arrow keys{fit}')
        elif c['i'] >= len(c['marks']):
            text = f'All points done ({n} clicked) - adjust if needed, then Save.{fit}'
        else:
            text = (f"Point {c['i'] + 1}/{len(c['marks'])}:  {c['marks'][c['i']][0]}"
                    f"   ({n} clicked, need ≥ 4){fit}")
        self.calib_label.config(text=text)
        self.draw_calib_map()
        self.render()

    def draw_calib_map(self):
        """Top view of the chosen court part, oriented as seen from that half's back line."""
        c, m = self.calib, self.calib_map
        m.delete('all')
        s = c['part'] or -1                       # viewing side: back line at the bottom
        zlo, zhi = (-HL, HL) if not c['part'] else sorted((0.0, s * HL))
        fmin, fmax = min(-s * zlo, -s * zhi) - 0.5, max(-s * zlo, -s * zhi) + 0.6
        pad = 8
        sc = min((MAP_W - 2 * pad) / (2 * HW + 1.0), (MAP_H - 2 * pad) / (fmax - fmin))
        X = lambda x: MAP_W / 2 + s * x * sc
        Y = lambda z: pad + (fmax - (-s * z)) * sc
        m.create_rectangle(X(-HW), Y(zlo), X(HW), Y(zhi), fill='#15977e', outline='')
        for _, (x1, z1), (x2, z2) in cl.COURT_LINES:
            a, b = max(min(z1, z2), zlo), min(max(z1, z2), zhi)
            if a <= b:
                m.create_line(X(x1), Y(a if z1 <= z2 else b), X(x2), Y(b if z1 <= z2 else a), fill='white')
        m.create_line(X(-HW - 0.2), Y(0), X(HW + 0.2), Y(0), fill='white', width=3, dash=(4, 2))
        m.create_text(MAP_W / 2, Y(0) - 7 if s < 0 or not c['part'] else Y(0) + 7, text='net', fill='white',
                      font=('TkDefaultFont', 8))
        cam_no = int(self.cam_var.get())
        sx, sz = CORNERS[cam_no - 1]
        if zlo <= sz * HL <= zhi:
            cx, cy = X(sx * (HW + 0.35)), Y(sz * (HL + 0.35))
            m.create_polygon(cx, cy - 6, cx - 5, cy + 4, cx + 5, cy + 4, fill='#ffe08a')
            m.create_text(min(max(cx, 22), MAP_W - 22), cy + 12 if cy < MAP_H - 14 else cy - 14,
                          text=f'Cam {cam_no}', fill='#ffe08a', font=('TkDefaultFont', 8, 'bold'))
        done = {i for i, _, _ in c['points']}
        sel = c['points'][c['sel']][0] if c['sel'] is not None else None
        self.map_xy = []
        for i, (_, (x, z)) in enumerate(c['marks']):
            px, py = X(x), Y(z)
            self.map_xy.append((px, py))
            if i == c['i'] or i == sel:
                m.create_oval(px - 7, py - 7, px + 7, py + 7, outline='#ff3b30' if i == c['i'] else '#ffffff', width=2)
                m.create_oval(px - 3, py - 3, px + 3, py + 3, fill='#ff3b30' if i not in done else '#3cff8f', outline='')
            else:
                color = '#3cff8f' if i in done else '#9aa' if i < c['i'] else '#ffffff'
                m.create_oval(px - 2.5, py - 2.5, px + 2.5, py + 2.5, fill=color, outline='')

    def on_map_click(self, ev):
        """Clicking a landmark on the map makes it the point to place (or selects its point)."""
        c = self.calib
        if not c or not getattr(self, 'map_xy', None):
            return
        k = min(range(len(self.map_xy)), key=lambda j: math.hypot(self.map_xy[j][0] - ev.x, self.map_xy[j][1] - ev.y))
        if math.hypot(self.map_xy[k][0] - ev.x, self.map_xy[k][1] - ev.y) > 10:
            return
        c['i'] = k
        c['sel'] = next((n for n, p in enumerate(c['points']) if p[0] == k), None)
        self.calib_prompt()

    def calib_skip(self):
        c = self.calib
        if c and c['i'] < len(c['marks']):
            c['i'], c['sel'] = self.calib_next_free(c['i']), None
            self.calib_prompt()

    def calib_undo(self):
        c = self.calib
        if c and c['points']:
            c['i'], c['sel'] = c['points'].pop()[0], None
            self.calib_prompt()

    def calib_delete(self, n=None):
        c = self.calib
        n = c['sel'] if c and n is None else n
        if c and n is not None:
            i = c['points'].pop(n)[0]
            c['sel'], c['i'] = None, min(c['i'], i)
            self.calib_prompt()

    def calib_nudge(self, ev, dx, dy, step):
        c = self.calib
        if not c or c['sel'] is None or isinstance(ev.widget, (tk.Entry, ttk.Entry, ttk.Combobox)):
            return
        p = c['points'][c['sel']]
        p[1] = min(max(p[1] + dx * step, 0), self.size[0])
        p[2] = min(max(p[2] + dy * step, 0), self.size[1])
        c['zoom'] = (p[1], p[2])
        self.calib_prompt()
        return 'break'

    def calib_cancel(self):
        if self.calib:
            self.calib = None
            self.calib_bar.pack_forget()
            self.render()

    def calib_finish(self):
        c = self.calib
        if not c or len(c['points']) < 4:
            self.set_status('Calibration needs at least 4 clicked landmarks.')
            return
        cam_no = int(self.cam_var.get())
        w, h = self.size
        marks = c['marks']
        pixels = [(u, v) for _, u, v in c['points']]
        world = [marks[i][1] for i, _, _ in c['points']]
        try:
            cam = CourtCamera.fit(cam_no, w, h, self.args.hfov, pixels, world)
        except (ValueError, cv2.error) as e:
            messagebox.showerror('Calibration', f'Calibration failed: {e}')
            return
        if cam.error > 8:
            if not messagebox.askyesno('Calibration', f'The clicks fit badly ({cam.error:.1f} px average error) - '
                                                      'probably a point clicked on the wrong crossing.\n\n'
                                                      'Save anyway? (No = keep clicking / Undo)'):
                return
        self.calib_path(cam_no).write_text(json.dumps({
            'cam': cam_no, 'image_size': [w, h], 'hfov': round(cam.hfov, 2), 'part': self.part_var.get(),
            'error_px': round(cam.error, 2),
            'points': [{'name': marks[i][0], 'world': marks[i][1], 'pixel': [round(u, 1), round(v, 1)]}
                       for i, u, v in c['points']]}, indent=2))
        self.calib_cancel()
        self.build_camera()
        self.set_status(f'Calibrated: {cam.label}. Saved {self.calib_path(cam_no).name}. '
                        f'Check that the cyan lines sit on the court lines, then Resume.')

    # -- Freeze / review ----------------------------------------------------------
    def freeze(self):
        self.frozen = True
        if self.worker:
            self.worker.paused.set()

    def resume(self):
        if self.calib:
            return
        self.frozen, self.review_fall = False, None
        if self.worker:
            self.worker.paused.clear()
        self.set_status('Running.')
        self.render()

    def review(self):
        if self.calib:
            return
        if self.frame is None or self.camera is None:
            self.set_status('Nothing to review yet - open a source first.')
            return
        self.freeze()
        sel = self.listbox.curselection()
        if sel:
            fall, pts3d = self.falls[sel[0]]
            pts3d = reconstruct(self.camera, fall, self.args.plane)   # current calibration
        elif self.falls:
            fall, _ = self.falls[-1]
            pts3d = reconstruct(self.camera, fall, self.args.plane)
        else:
            fall = self.worker.tracker.current_fall() if self.worker and self.worker.tracker else None
            pts3d = reconstruct(self.camera, fall, self.args.plane) if fall else None
        if not fall:
            self.set_status('Video frozen. No saved fall and no shuttle track yet - Resume and wait for a landing.')
            self.render()
            return
        self.review_fall = fall
        self.render()
        if not pts3d:
            self.set_status('Video frozen. The landing pixel is above the horizon for this camera model - calibrate.')
            return
        # Hand the fall to the 3D court page (tripods.html / index.html + shuttle.js).
        self.review_seq += 1
        record = dict(self.fall_record(fall, pts3d), seq=self.review_seq, slowmo=self.args.slowmo)
        live = HERE / 'reviews' / 'latest_review.json'
        live.parent.mkdir(exist_ok=True)
        tmp = live.with_suffix('.tmp')
        tmp.write_text(json.dumps(record))
        tmp.replace(live)                     # the page never reads a half-written file
        if not self.browser_opened:
            self.open_3d()
        land = record['landing']
        self.set_status(f"Video frozen · {'live track' if fall.estimated else f'fall #{fall.id}'} replaying in the "
                        f"3D court ({self.web_url()}) · landing ({land['x']:.2f}, {land['z']:.2f}) {land['doubles']}"
                        f" · Space or Resume to continue")

    def web_url(self):
        return f'http://localhost:{self.web_port}/{self.args.page}.html?review=live'

    def open_3d(self):
        """Opens the 3D court page; it replays every Review from then on (keep the tab open)."""
        self.browser_opened = True
        webbrowser.open(self.web_url())

    # -- Falls ------------------------------------------------------------------------
    def fall_record(self, fall, pts3d):
        """The JSON saved for a fall (and sent to the 3D page)."""
        land = landing_of(pts3d)
        record = {'id': fall.id, 'estimated': fall.estimated, 'time_s': round(fall.points[fall.contact].t, 3),
                  'frame': fall.points[fall.contact].frame, 'cam': int(self.cam_var.get()),
                  'camera': self.camera.label if self.camera else None,
                  'camera_pos': [round(float(a), 3) for a in self.camera.pos] if self.camera else None,
                  'plane': self.args.plane, 'image': Path(fall.image).name if fall.image else None,
                  'landing': None, 'trajectory': pts3d or [
                      {'frame': d.frame, 't': round(d.t, 4), 'u': round(d.u, 1), 'v': round(d.v, 1),
                       'conf': round(d.conf, 3)} for d in fall.points]}
        if land:
            res = cl.judge(land['x'], land['z'])
            c = fall.points[fall.contact]          # bbox bottom centre = floor contact pixel
            record['landing'] = {'x': land['x'], 'z': land['z'], 'pixel': [round(c.u, 1), round(c.foot, 1)],
                                 'doubles': 'IN' if res['doubles'] else 'OUT',
                                 'singles': 'IN' if res['singles'] else 'OUT'}
        return record

    def add_fall(self, fall, frame):
        fall.id = len(self.falls) + 1
        pts3d = reconstruct(self.camera, fall, self.args.plane) if self.camera else None
        self.falls.append((fall, pts3d))
        snap = frame.copy()
        draw_fall_2d(snap, fall)
        fall.image = str(self.session / f'fall_{fall.id:03d}.jpg')
        cv2.imwrite(fall.image, snap)
        record = self.fall_record(fall, pts3d)
        land = record['landing']
        (self.session / f'fall_{fall.id:03d}.json').write_text(json.dumps(record, indent=2))
        if land:
            label = f"#{fall.id:<3} {record['time_s']:7.2f}s  ({land['x']:+.2f}, {land['z']:+.2f})  {land['doubles']}"
        else:
            label = f'#{fall.id:<3} {record["time_s"]:7.2f}s  (not on floor)'
        self.listbox.insert('end', label)
        self.set_status(f'Fall #{fall.id} saved: {label.split("s", 1)[1].strip()}  → {fall.image}')

    # -- Frame loop -------------------------------------------------------------------
    def poll(self):
        w = self.worker
        if w:
            try:
                while True:
                    ev = w.events.get_nowait()
                    kind = ev[0]
                    if kind == 'info':
                        self.size = (ev[1], ev[2])
                        self.build_camera()
                        self.set_status(f'Running · {ev[1]}x{ev[2]} @ {ev[3]:.0f} fps · '
                                        f'{"live" if w.live else "video file"}')
                    elif kind == 'fall':
                        self.add_fall(ev[1], ev[2])
                    elif kind == 'status':
                        self.set_status(ev[1])
                    elif kind == 'eof':
                        self.set_status('End of video. Press Review to see the last fall in 3D.')
                    elif kind == 'error':
                        self.set_status('Error: ' + ev[1])
                        messagebox.showerror('Shuttle review', ev[1])
            except queue.Empty:
                pass
            item = None
            try:
                while True:
                    item = w.frames.get_nowait()
            except queue.Empty:
                pass
            if item is not None and not self.frozen:
                frame, idx, t, dets, tail = item
                self.frame, self.overlay = frame, (dets, tail)
                self.render()
        self.root.after(15, self.poll)

    def render(self):
        if self.frame is None:
            return
        img = self.frame.copy()
        # While calibrating, the court lines come from the clicks so far (once there are 4).
        overlay_cam = self.calib['guess'] if self.calib and self.calib.get('guess') else self.camera
        if overlay_cam and self.court_var.get() and not (self.calib and not self.calib.get('guess')):
            draw_court_2d(img, overlay_cam)
        if self.review_fall:
            draw_fall_2d(img, self.review_fall)
        elif not self.frozen:
            dets, tail = self.overlay
            for d in dets:
                cv2.rectangle(img, (int(d.u - d.w / 2), int(d.v - d.h / 2)), (int(d.u + d.w / 2), int(d.foot)),
                              (0, 255, 0), 1)
            if len(tail) > 1:
                cv2.polylines(img, [np.int32(tail)], False, (0, 165, 255), 2, cv2.LINE_AA)
        if self.calib:
            self.draw_calib_points(img)
        if self.frozen:
            cv2.putText(img, 'FROZEN', (20, 40), cv2.FONT_HERSHEY_SIMPLEX, 1.1, (0, 0, 255), 3)
        cw, ch = max(self.canvas.winfo_width(), 1), max(self.canvas.winfo_height(), 1)
        h, w = img.shape[:2]
        s = min(cw / w, ch / h)
        dw, dh = max(1, int(w * s)), max(1, int(h * s))
        ox, oy = (cw - dw) // 2, (ch - dh) // 2
        self.view = (s, ox, oy)
        img = cv2.resize(img, (dw, dh), interpolation=cv2.INTER_AREA if s < 1 else cv2.INTER_LINEAR)
        self.photo = ImageTk.PhotoImage(Image.fromarray(cv2.cvtColor(img, cv2.COLOR_BGR2RGB)))
        self.canvas.delete('all')
        self.canvas.create_image(ox, oy, anchor='nw', image=self.photo)

    def draw_calib_points(self, img):
        c = self.calib
        guess, resid = c.get('guess'), c.get('resid', {})
        for n, (i, u, v) in enumerate(c['points']):
            r = resid.get(i)
            color = (0, 255, 0) if r is None or r < 3 else (0, 165, 255) if r < 8 else (0, 0, 255)
            if guess and r is not None and r >= 1:      # line to where the fitted camera puts it
                x, z = c['marks'][i][1]
                p = guess.project((x, cl.FLOOR_Y, z))
                if p:
                    cv2.line(img, (int(u), int(v)), (int(p[0]), int(p[1])), color, 1, cv2.LINE_AA)
            cv2.drawMarker(img, (int(u), int(v)), color, cv2.MARKER_CROSS, 18, 2)
            if n == c['sel']:
                cv2.circle(img, (int(u), int(v)), 13, (255, 255, 255), 2, cv2.LINE_AA)
            cv2.putText(img, str(i + 1), (int(u) + 8, int(v) - 8), cv2.FONT_HERSHEY_SIMPLEX, 0.6, color, 2)
        if guess and c['sel'] is None and c['i'] < len(c['marks']):
            x, z = c['marks'][c['i']][1]
            uv = guess.project((x, cl.FLOOR_Y, z))
            if uv and 0 <= uv[0] <= img.shape[1] and 0 <= uv[1] <= img.shape[0]:
                cv2.circle(img, (int(uv[0]), int(uv[1])), 16, (0, 255, 255), 2, cv2.LINE_AA)
                cv2.putText(img, f"{c['i'] + 1}?", (int(uv[0]) + 18, int(uv[1]) + 6),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 255), 2)
        if c.get('zoom'):                                 # magnifier: 6x around the mouse / dragged point
            u, v = int(c['zoom'][0]), int(c['zoom'][1])
            h, w = img.shape[:2]
            r, z = 30, 6
            src = cv2.copyMakeBorder(img, r, r, r, r, cv2.BORDER_CONSTANT)[v:v + 2 * r, u:u + 2 * r]
            mag = cv2.resize(src, (2 * r * z, 2 * r * z), interpolation=cv2.INTER_NEAREST)
            m = mag.shape[0]
            cv2.line(mag, (m // 2, 0), (m // 2, m), (0, 0, 255), 1)
            cv2.line(mag, (0, m // 2), (m, m // 2), (0, 0, 255), 1)
            cv2.rectangle(mag, (0, 0), (m - 1, m - 1), (255, 255, 255), 2)
            x0 = 10 if u > w / 2 else w - m - 10          # the corner away from the mouse
            img[10:10 + m, x0:x0 + m] = mag

    # -- Calibration mouse: click = place, drag = move, right-click = delete -------
    def to_frame(self, ev):
        s, ox, oy = self.view
        u, v = (ev.x - ox) / s, (ev.y - oy) / s
        return (u, v) if self.size and 0 <= u <= self.size[0] and 0 <= v <= self.size[1] else None

    def point_near(self, uv, radius_px=12):
        """Index (in calib['points']) of the clicked point within `radius_px` screen pixels."""
        c = self.calib
        if not c['points']:
            return None
        n = min(range(len(c['points'])), key=lambda k: math.hypot(c['points'][k][1] - uv[0], c['points'][k][2] - uv[1]))
        d = math.hypot(c['points'][n][1] - uv[0], c['points'][n][2] - uv[1]) * self.view[0]
        return n if d <= radius_px else None

    def on_press(self, ev):
        c = self.calib
        uv = self.to_frame(ev) if c else None
        if uv is None:
            return
        self.canvas.focus_set()            # arrow keys go to the video, not a text box
        n = self.point_near(uv)
        if n is not None:                  # grab an existing point
            c['sel'], c['drag'] = n, True
            c['i'] = self.calib_next_free(-1)
        elif c['i'] < len(c['marks']):     # place the current landmark (replaces an older click of it)
            c['points'] = [p for p in c['points'] if p[0] != c['i']]
            c['points'].append([c['i'], *uv])
            c['sel'] = None
            c['i'] = self.calib_next_free(c['i'])
        c['zoom'] = uv
        self.calib_prompt()

    def on_drag(self, ev):
        c = self.calib
        uv = self.to_frame(ev) if c else None
        if uv is None or not c['drag'] or c['sel'] is None:
            return
        c['points'][c['sel']][1:] = uv
        c['zoom'] = uv
        now = time.monotonic()
        if now - c.get('last_drag', 0) > 0.04:     # refit at most ~25x a second while dragging
            c['last_drag'] = now
            self.calib_prompt(quick=True)

    def on_release(self, ev):
        c = self.calib
        if c and c['drag']:
            c['drag'] = False
            self.calib_prompt()            # full fit (lens FOV too) once the point is dropped

    def on_right_click(self, ev):
        c = self.calib
        uv = self.to_frame(ev) if c else None
        n = self.point_near(uv) if uv else None
        if n is not None:
            self.calib_delete(n)

    def on_motion(self, ev):
        c = self.calib
        if not c:
            return
        uv = self.to_frame(ev)
        if uv != c['zoom']:
            c['zoom'] = uv
            now = time.monotonic()
            if now - c.get('last_move', 0) > 0.03:
                c['last_move'] = now
                self.render()


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--source', help='webcam index (0), video file, or stream URL')
    ap.add_argument('--model', help='YOLO weights (default: best.pt next to this file or ../sample_folder/best.pt)')
    ap.add_argument('--cam', type=int, default=1, choices=[1, 2, 3, 4], help='which tripod camera this video is')
    ap.add_argument('--conf', type=float, default=0.5, help='detection confidence (default 0.5)')
    ap.add_argument('--imgsz', type=int, default=1280, help='YOLO inference size (default 1280)')
    ap.add_argument('--device', help="YOLO device, e.g. 0 or cpu (default: auto)")
    ap.add_argument('--plane', choices=['view', 'length'], default='view',
                    help='vertical plane assumed for the flight (default view)')
    ap.add_argument('--slowmo', type=float, default=0.5, help='3D replay speed, 1 = real time (default 0.5)')
    ap.add_argument('--page', choices=['tripods', 'index'], default='tripods',
                    help='3D court page used for Review (default tripods)')
    ap.add_argument('--port', type=int, default=8000, help='local web server port for the 3D page (default 8000)')
    ap.add_argument('--out', default=str(HERE / 'reviews'), help='folder for saved sessions (default ./reviews)')
    g = ap.add_argument_group('tripod model (used until the camera is calibrated; same as camera_link.py)')
    g.add_argument('--distance', type=float, default=1.20)
    g.add_argument('--height', type=float, default=1.20)
    g.add_argument('--angle', type=float, default=45)
    g.add_argument('--tilt', type=float, default=27)
    g.add_argument('--hfov', type=float, default=75, help='horizontal lens FOV, also used for calibration')
    return ap.parse_args(argv)


def main():
    root = tk.Tk()
    App(root, parse_args())
    root.mainloop()


if __name__ == '__main__':
    main()
