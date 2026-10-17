#!/usr/bin/env python3
"""Link court co-ordinates with the four tripod cameras.

Uses the same conventions as index.html / tripods.html:
  1 unit = 1 metre, X = width, Y = height, Z = length, net centre = (0, 0, 0).
  Cam 1 (-X,-Z), Cam 2 (+X,-Z), Cam 3 (+X,+Z), Cam 4 (-X,+Z).

Each camera sits `distance` m out from its court corner (along the diagonal),
lens `height` m above the floor, facing into the court at `angle` degrees from
the back boundary line, and tilted down by `tilt` degrees.

Commands
  point X Z          which cameras see court point (X, Z), and at which pixel
  pixel CAM U V      court point (X, Z) seen at pixel (U, V) of camera CAM (1-4)
  lines              how much of every court line each camera sees, plus the
                     tilt range that keeps both the near margin line and the
                     far back line in frame

Examples
  python3 camera_link.py point 1.20 -4.50
  python3 camera_link.py pixel 1 960 700
  python3 camera_link.py --distance 2 --height 2.2 lines
  python3 camera_link.py point 0 6.7 --json
"""
import argparse
import json
import math

# ---- Court (same numbers as the HTML pages) --------------------------------
WIDTH = 6.10
LENGTH = 13.40
SINGLES_WIDTH = 5.18
HALF_W = WIDTH / 2
HALF_L = LENGTH / 2
FLOOR_Y = 0.053          # painted line height in the 3D scene
LENS_FORWARD = 0.15      # lens sits 0.15 m in front of the tripod centre


def net_height(x):
    return 1.524 + (1.55 - 1.524) * (abs(x) / HALF_W) ** 1.6


# Court lines as (name, (x1, z1), (x2, z2)).
COURT_LINES = [
    ('left doubles sideline', (-HALF_W, -HALF_L), (-HALF_W, HALF_L)),
    ('right doubles sideline', (HALF_W, -HALF_L), (HALF_W, HALF_L)),
    ('left singles sideline', (-SINGLES_WIDTH / 2, -HALF_L), (-SINGLES_WIDTH / 2, HALF_L)),
    ('right singles sideline', (SINGLES_WIDTH / 2, -HALF_L), (SINGLES_WIDTH / 2, HALF_L)),
    ('back boundary -Z', (-HALF_W, -HALF_L), (HALF_W, -HALF_L)),
    ('back boundary +Z', (-HALF_W, HALF_L), (HALF_W, HALF_L)),
    ('long service (doubles) -Z', (-HALF_W, -HALF_L + 0.76), (HALF_W, -HALF_L + 0.76)),
    ('long service (doubles) +Z', (-HALF_W, HALF_L - 0.76), (HALF_W, HALF_L - 0.76)),
    ('short service -Z', (-HALF_W, -1.98), (HALF_W, -1.98)),
    ('short service +Z', (-HALF_W, 1.98), (HALF_W, 1.98)),
    ('centre line -Z', (0, -HALF_L), (0, -1.98)),
    ('centre line +Z', (0, 1.98), (0, HALF_L)),
]


# ---- Small vector helpers ---------------------------------------------------
def add(a, b): return (a[0] + b[0], a[1] + b[1], a[2] + b[2])
def sub(a, b): return (a[0] - b[0], a[1] - b[1], a[2] - b[2])
def scale(a, s): return (a[0] * s, a[1] * s, a[2] * s)
def dot(a, b): return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def cross(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def norm(a):
    n = math.sqrt(dot(a, a))
    return (a[0] / n, a[1] / n, a[2] / n)


# ---- Camera model -------------------------------------------------------------
class Camera:
    def __init__(self, index, sx, sz, distance, height, angle_deg, tilt_deg,
                 hfov_deg, width_px, height_px):
        self.index = index
        self.name = f'Cam {index}'
        self.sx, self.sz = sx, sz
        self.height = height
        a = math.radians(angle_deg)
        t = math.radians(tilt_deg)
        off = distance / math.sqrt(2)
        # Horizontal facing direction, exactly as in tripods.html.
        self.dir2 = (-sx * math.cos(a), -sz * math.sin(a))
        base = (sx * (HALF_W + off), 0.0, sz * (HALF_L + off))
        self.pos = (base[0] + self.dir2[0] * LENS_FORWARD, height, base[2] + self.dir2[1] * LENS_FORWARD)

        self.forward = norm((self.dir2[0] * math.cos(t), -math.sin(t), self.dir2[1] * math.cos(t)))
        self.right = norm(cross(self.forward, (0.0, 1.0, 0.0)))
        self.up = cross(self.right, self.forward)

        self.w, self.h = width_px, height_px
        self.f = (width_px / 2) / math.tan(math.radians(hfov_deg) / 2)   # focal length in px
        self.vfov = math.degrees(2 * math.atan((height_px / 2) / self.f))

    def project(self, p):
        """World point -> (u, v, depth). u/v are None if the point is behind the camera."""
        d = sub(p, self.pos)
        zc = dot(d, self.forward)
        if zc <= 0.05:
            return None, None, zc
        u = self.w / 2 + self.f * dot(d, self.right) / zc
        v = self.h / 2 - self.f * dot(d, self.up) / zc
        return u, v, zc

    def in_frame(self, u, v):
        return u is not None and 0 <= u <= self.w and 0 <= v <= self.h

    def unproject_to_floor(self, u, v):
        """Pixel -> court (X, Z) on the floor, or None if the pixel is above the horizon."""
        a = (u - self.w / 2) / self.f
        b = -(v - self.h / 2) / self.f
        ray = add(self.forward, add(scale(self.right, a), scale(self.up, b)))
        if ray[1] >= -1e-9:
            return None
        s = (FLOOR_Y - self.pos[1]) / ray[1]
        hit = add(self.pos, scale(ray, s))
        return hit[0], hit[2]

    def looks_through_net(self, x, z):
        """True if the sight line to (x, z) passes through the net (below the tape)."""
        cz = self.pos[2]
        if cz * z >= 0:      # same side of the net
            return False
        k = cz / (cz - z)    # fraction of the way where the sight line crosses z = 0
        x0 = self.pos[0] + (x - self.pos[0]) * k
        y0 = self.pos[1] + (FLOOR_Y - self.pos[1]) * k
        return abs(x0) <= HALF_W and y0 < net_height(x0)


def build_cameras(args):
    corners = [(-1, -1), (1, -1), (1, 1), (-1, 1)]
    return [Camera(i + 1, sx, sz, args.distance, args.height, args.angle, args.tilt,
                   args.hfov, args.res[0], args.res[1])
            for i, (sx, sz) in enumerate(corners)]


def judge(x, z):
    in_len = abs(z) <= HALF_L
    return {'doubles': in_len and abs(x) <= HALF_W,
            'singles': in_len and abs(x) <= SINGLES_WIDTH / 2}


# ---- Commands -----------------------------------------------------------------
def cmd_point(cams, x, z):
    res = judge(x, z)
    out = {'point': {'x': x, 'z': z, 'doubles': 'IN' if res['doubles'] else 'OUT',
                     'singles': 'IN' if res['singles'] else 'OUT'},
           'cameras': []}
    for c in cams:
        u, v, depth = c.project((x, FLOOR_Y, z))
        visible = c.in_frame(u, v)
        out['cameras'].append({
            'camera': c.index,
            'visible': visible,
            'u': round(u, 1) if u is not None else None,
            'v': round(v, 1) if v is not None else None,
            'distance_m': round(math.dist(c.pos, (x, FLOOR_Y, z)), 2),
            'through_net': visible and c.looks_through_net(x, z),
        })
    out['seen_by'] = [r['camera'] for r in out['cameras'] if r['visible']]
    return out


def cmd_pixel(cams, cam_no, u, v):
    c = cams[cam_no - 1]
    hit = c.unproject_to_floor(u, v)
    if hit is None:
        return {'camera': cam_no, 'u': u, 'v': v, 'on_floor': False}
    x, z = hit
    res = judge(x, z)
    # Which other cameras would also see this spot.
    others = [o.index for o in cams if o.index != cam_no and o.in_frame(*o.project((x, FLOOR_Y, z))[:2])]
    return {'camera': cam_no, 'u': u, 'v': v, 'on_floor': True,
            'x': round(x, 3), 'z': round(z, 3),
            'doubles': 'IN' if res['doubles'] else 'OUT',
            'singles': 'IN' if res['singles'] else 'OUT',
            'also_seen_by': others}


def line_coverage(c, p1, p2, samples=200):
    seen = 0
    for i in range(samples + 1):
        k = i / samples
        x = p1[0] + (p2[0] - p1[0]) * k
        z = p1[1] + (p2[1] - p1[1]) * k
        u, v, _ = c.project((x, FLOOR_Y, z))
        if c.in_frame(u, v):
            seen += 1
    return seen / (samples + 1)


def axis_track(c):
    """Where the camera's ground track (its horizontal facing line) enters and leaves the court."""
    px, pz = c.pos[0], c.pos[2]
    dx, dz = c.dir2
    t_near, t_far = -math.inf, math.inf
    for p, d, lo, hi in ((px, dx, -HALF_W, HALF_W), (pz, dz, -HALF_L, HALF_L)):
        if abs(d) < 1e-12:
            if not lo <= p <= hi:
                return None
            continue
        t1, t2 = (lo - p) / d, (hi - p) / d
        t_near, t_far = max(t_near, min(t1, t2)), min(t_far, max(t1, t2))
    if t_near > t_far or t_far <= 0:
        return None
    return max(t_near, 0.0), t_far


def tilt_window(c, margin_deg=1.0):
    """Tilt range (deg) where the near margin line and the far back line are both on the image centre column."""
    track = axis_track(c)
    if track is None:
        return None
    near, far = track
    h = c.pos[1] - FLOOR_Y
    half_v = c.vfov / 2
    min_tilt = math.degrees(math.atan2(h, near)) - half_v + margin_deg
    max_tilt = math.degrees(math.atan2(h, far)) + half_v - margin_deg
    return {'near_line_m': round(near, 2), 'far_line_m': round(far, 2),
            'min_tilt': round(min_tilt, 1), 'max_tilt': round(max_tilt, 1),
            'possible': min_tilt <= max_tilt}


def cmd_lines(cams):
    out = []
    for c in cams:
        near_back = 'back boundary -Z' if c.sz < 0 else 'back boundary +Z'
        near_side = 'left doubles sideline' if c.sx < 0 else 'right doubles sideline'
        lines = {name: round(line_coverage(c, p1, p2) * 100) for name, p1, p2 in COURT_LINES}
        out.append({'camera': c.index, 'near_back_line': near_back, 'near_sideline': near_side,
                    'coverage_pct': lines, 'tilt_window': tilt_window(c)})
    return out


# ---- Output -------------------------------------------------------------------
def print_point(r):
    p = r['point']
    print(f"Point ({p['x']:.2f}, {p['z']:.2f})  doubles {p['doubles']}, singles {p['singles']}")
    for c in r['cameras']:
        if c['visible']:
            note = '  (through the net)' if c['through_net'] else ''
            print(f"  Cam {c['camera']}: VISIBLE  pixel ({c['u']:.0f}, {c['v']:.0f})  {c['distance_m']:.2f} m{note}")
        else:
            where = 'behind camera' if c['u'] is None else f"off frame ({c['u']:.0f}, {c['v']:.0f})"
            print(f"  Cam {c['camera']}: not visible  {where}")
    print(f"Seen by: {', '.join(f'Cam {i}' for i in r['seen_by']) or 'no camera'}")


def print_pixel(r):
    if not r['on_floor']:
        print(f"Cam {r['camera']} pixel ({r['u']:.0f}, {r['v']:.0f}) is above the horizon, not on the floor.")
        return
    print(f"Cam {r['camera']} pixel ({r['u']:.0f}, {r['v']:.0f}) -> court ({r['x']:.2f}, {r['z']:.2f})"
          f"  doubles {r['doubles']}, singles {r['singles']}")
    print(f"Also seen by: {', '.join(f'Cam {i}' for i in r['also_seen_by']) or 'no other camera'}")


def print_lines(rows, tilt):
    for r in rows:
        print(f"Cam {r['camera']}  (tilt {tilt:g}°)")
        for name, pct in r['coverage_pct'].items():
            mark = ' <- near margin' if name in (r['near_back_line'], r['near_sideline']) else ''
            print(f"  {name:28s} {pct:3d}% in frame{mark}")
        w = r['tilt_window']
        if w:
            verdict = f"use tilt {w['min_tilt']}° .. {w['max_tilt']}°" if w['possible'] else \
                f"NOT possible (need >= {w['min_tilt']}° for near line, <= {w['max_tilt']}° for far line)"
            print(f"  centre column: near margin {w['near_line_m']} m, far back line {w['far_line_m']} m -> {verdict}")
        print()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--distance', type=float, default=1.20, help='corner -> tripod, m (default 1.20)')
    ap.add_argument('--height', type=float, default=1.20, help='floor -> lens, m (default 1.20)')
    ap.add_argument('--angle', type=float, default=45, help='from back boundary line, deg (default 45)')
    ap.add_argument('--tilt', type=float, default=27, help='camera tilted down, deg (default 27)')
    ap.add_argument('--hfov', type=float, default=75, help='horizontal field of view, deg (default 75)')
    ap.add_argument('--res', type=int, nargs=2, default=[1920, 1080], metavar=('W', 'H'),
                    help='image size in pixels (default 1920 1080)')
    ap.add_argument('--json', action='store_true', help='print JSON instead of text')
    sub_p = ap.add_subparsers(dest='cmd', required=True)
    p = sub_p.add_parser('point', help='court point -> camera pixels')
    p.add_argument('x', type=float)
    p.add_argument('z', type=float)
    p = sub_p.add_parser('pixel', help='camera pixel -> court point')
    p.add_argument('cam', type=int, choices=[1, 2, 3, 4])
    p.add_argument('u', type=float)
    p.add_argument('v', type=float)
    sub_p.add_parser('lines', help='court line visibility and tilt window per camera')
    args = ap.parse_args()

    cams = build_cameras(args)
    if args.cmd == 'point':
        result = cmd_point(cams, args.x, args.z)
        printer = print_point
    elif args.cmd == 'pixel':
        result = cmd_pixel(cams, args.cam, args.u, args.v)
        printer = print_pixel
    else:
        result = cmd_lines(cams)
        printer = lambda r: print_lines(r, args.tilt)

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        printer(result)


if __name__ == '__main__':
    main()
