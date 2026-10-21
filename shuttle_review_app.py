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

# Floor points that can be clicked to calibrate a camera: (name, (x, z)).
LANDMARKS = [
    ('court corner -X -Z (Cam 1 corner)', (-HW, -HL)),
    ('court corner +X -Z (Cam 2 corner)', (HW, -HL)),
    ('court corner +X +Z (Cam 3 corner)', (HW, HL)),
    ('court corner -X +Z (Cam 4 corner)', (-HW, HL)),
    ('net post foot -X', (-HW, 0.0)),
    ('net post foot +X', (HW, 0.0)),
    ('short service line x -X sideline, -Z half', (-HW, -1.98)),
    ('short service line x +X sideline, -Z half', (HW, -1.98)),
    ('short service line x +X sideline, +Z half', (HW, 1.98)),
    ('short service line x -X sideline, +Z half', (-HW, 1.98)),
    ('centre line x short service line, -Z half', (0.0, -1.98)),
    ('centre line x short service line, +Z half', (0.0, 1.98)),
    ('centre line x back line -Z', (0.0, -HL)),
    ('centre line x back line +Z', (0.0, HL)),
]


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
        ok, rvec, tvec = cv2.solvePnP(obj, img, K, None, flags=cv2.SOLVEPNP_IPPE)
        if not ok:
            raise ValueError('solvePnP failed')
        ok, rvec, tvec = cv2.solvePnP(obj, img, K, None, rvec, tvec, True, cv2.SOLVEPNP_ITERATIVE)
        Rcw, _ = cv2.Rodrigues(rvec)
        pos = (-Rcw.T @ tvec).ravel()
        if pos[1] <= cl.FLOOR_Y:
            raise ValueError('camera came out below the floor - check the clicked points')
        proj, _ = cv2.projectPoints(obj, rvec, tvec, K, None)
        err = float(np.sqrt(np.mean(np.sum((proj.reshape(-1, 2) - img) ** 2, axis=1))))
        cam = cls(K, Rcw.T, pos, (w, h), f'Cam {cam_no}: calibrated, {len(obj)} points, {err:.1f} px error')
        cam.error = err
        return cam

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
        ttk.Button(top, text='Calibrate', command=self.calib_start).pack(side='left', padx=(4, 14))
        self.court_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(top, text='Court overlay', variable=self.court_var, command=self.render).pack(side='left')
        ttk.Button(top, text='▶ Resume', command=self.resume).pack(side='right')
        ttk.Button(top, text='⏸ Review', command=self.review).pack(side='right', padx=4)
        ttk.Button(top, text='Open 3D view', command=self.open_3d).pack(side='right', padx=(14, 0))

        self.calib_bar = ttk.Frame(r, padding=6)
        self.calib_label = ttk.Label(self.calib_bar, font=('TkDefaultFont', 11, 'bold'))
        self.calib_label.pack(side='left')
        for text, cmd in (('Cancel', self.calib_cancel), ('Finish', self.calib_finish),
                          ('Undo', self.calib_undo), ('Skip point', self.calib_skip)):
            ttk.Button(self.calib_bar, text=text, command=cmd).pack(side='right', padx=2)

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
        self.canvas.bind('<Button-1>', self.on_click)
        self.canvas.bind('<Configure>', lambda e: self.render())
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
                    self.camera = CourtCamera.from_points(cam_no, w, h, c['hfov'],
                                                          [p['pixel'] for p in c['points']],
                                                          [p['world'] for p in c['points']])
                else:
                    self.set_status(f'{path.name} is for {c["image_size"]}, video is {w}x{h} - using tripod model')
            except (ValueError, KeyError, json.JSONDecodeError) as e:
                self.set_status(f'Ignoring {path.name}: {e}')
        self.cam_label.config(text=self.camera.label + ('' if 'calibrated' in self.camera.label else
                                                        '\nPress Calibrate and click court landmarks for real positions.'))
        self.render()

    def calib_start(self):
        if self.frame is None:
            self.set_status('Open a source first.')
            return
        self.freeze()
        self.calib = {'i': 0, 'points': []}
        self.calib_bar.pack(side='top', fill='x', after=self.root.winfo_children()[0])
        self.calib_prompt()

    def calib_prompt(self):
        c = self.calib
        if c['i'] >= len(LANDMARKS):
            self.calib_label.config(text=f"All landmarks done ({len(c['points'])} clicked) - press Finish.")
        else:
            self.calib_label.config(text=f"Click: {LANDMARKS[c['i']][0]}   ({len(c['points'])} clicked, "
                                         f"need ≥ 4 · Skip if not visible)")
        self.render()

    def calib_skip(self):
        if self.calib and self.calib['i'] < len(LANDMARKS):
            self.calib['i'] += 1
            self.calib_prompt()

    def calib_undo(self):
        if self.calib and self.calib['points']:
            self.calib['i'] = self.calib['points'].pop()[0]
            self.calib_prompt()

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
        pixels = [(u, v) for _, u, v in c['points']]
        world = [LANDMARKS[i][1] for i, _, _ in c['points']]
        try:
            cam = CourtCamera.from_points(cam_no, w, h, self.args.hfov, pixels, world)
        except (ValueError, cv2.error) as e:
            messagebox.showerror('Calibration', f'Calibration failed: {e}')
            return
        self.calib_path(cam_no).write_text(json.dumps({
            'cam': cam_no, 'image_size': [w, h], 'hfov': self.args.hfov,
            'points': [{'name': LANDMARKS[i][0], 'world': LANDMARKS[i][1], 'pixel': [round(u, 1), round(v, 1)]}
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
        if self.camera and self.court_var.get():
            draw_court_2d(img, self.camera)
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
            for n, (i, u, v) in enumerate(self.calib['points']):
                cv2.drawMarker(img, (int(u), int(v)), (0, 255, 255), cv2.MARKER_CROSS, 18, 2)
                cv2.putText(img, str(n + 1), (int(u) + 8, int(v) - 8), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 255), 2)
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

    def on_click(self, ev):
        if not self.calib or not self.size or self.calib['i'] >= len(LANDMARKS):
            return
        s, ox, oy = self.view
        u, v = (ev.x - ox) / s, (ev.y - oy) / s
        if not (0 <= u <= self.size[0] and 0 <= v <= self.size[1]):
            return
        self.calib['points'].append((self.calib['i'], u, v))
        self.calib['i'] += 1
        self.calib_prompt()


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
