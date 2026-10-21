BADMINTON 3D COURT - STEP 1

1. Extract this folder (if downloaded as a ZIP).
2. Open a terminal inside the folder.
3. Run: python3 -m http.server 8000
   Windows alternative: py -m http.server 8000
4. Visit: http://localhost:8000
5. Internet access is needed to load Three.js from the CDN.

Mouse: left-drag rotates, mouse wheel zooms, right-drag pans.
3D View / Top View buttons are at the top right.

SHUTTLE DROP (panel at top right)
- Point: click anywhere on the court (or its coloured border) and a shuttle
  drops onto that spot from low (0.30 m), gives a small bounce and settles
  on its side. A ring marks the spot
  (green = IN, red = OUT) and a tag shows the co-ordinates.
- Co-ordinates: type X and Z in metres and press "Drop shuttle".
  Origin (0, 0) is the centre of the net.
    X = across the width:   -3.05 ... 3.05  (doubles sidelines)
    Z = along the length:   -6.70 ... 6.70  (back boundary lines)
  Values up to 0.55 m beyond the lines are allowed (shown as OUT).
- Clear marks removes all landing marks.
The shuttle is drawn 1.5x real size so it is visible from the camera.

TRIPOD SETUP (separate page)
Visit: http://localhost:8000/tripods.html
- Four camera tripods, one off each corner of the court.
- Default: 1.20 m from the court corner (outward along the diagonal),
  camera lens 1.20 m above the floor, facing into the court at 45 degrees
  to the back boundary line, tilted 27 degrees down with a 75 degree lens, so the
  near court corner and floor margin are at the bottom of the frame and the
  far back line is still in view (25-29 degrees keeps both).
- Distance, height, angle, tilt and lens FOV can be changed in the panel ("Apply").
- The same Shuttle drop panel as the main page: click the court (also inside
  a Cam view) or type co-ordinates.
- Cam 1-4 buttons show the view from each camera's lens (same camera model
  camera_link.py uses).

CAMERA <-> CO-ORDINATE LINK (Python, no extra packages)
  python3 camera_link.py point 1.20 -4.50   which cameras see (X, Z), at which pixel
  python3 camera_link.py pixel 1 960 700    Cam 1 pixel -> court (X, Z)
  python3 camera_link.py lines              line visibility + usable tilt range
Options: --distance --height --angle --tilt --hfov --res W H --json

SHUTTLE REVIEW APP (Python desktop app, YOLO best.pt)
  pip install -r requirements.txt
  python3 shuttle_review_app.py --source 0                  webcam 0
  python3 shuttle_review_app.py --source match.mp4 --cam 2  video file, Cam 2
  python3 shuttle_review_app.py --source rtsp://...         live stream
  Model: --model path/to/best.pt (default: best.pt next to the script, then
  ../sample_folder/best.pt). Also --conf 0.5 --imgsz 1280 --device 0|cpu.
- Every frame goes through YOLO; detections are linked into tracks. When a
  track moves and then comes to rest, it is saved as a fall.
- Saved per session in reviews/<date>_<source>/:
    track.csv       every detection (frame, time, track id, pixel, box, conf)
    fall_###.json   the fall: pixel + court (x, y, z) per frame, landing X/Z,
                    doubles/singles IN/OUT
    fall_###.jpg    the frame with the trajectory drawn
- Review (or Space): freezes the video, draws the trajectory on the frame and
  replays the fall on the 3D court of tripods.html (--page index for
  index.html), using shuttle.js. The app serves this folder itself on
  http://localhost:8000 (--port), so no separate http.server is needed, and
  opens http://localhost:8000/tripods.html?review=live in the browser. Keep
  that tab open: every Review replays there (the page polls
  reviews/latest_review.json). The page moves its view to the landing spot;
  "Replay recorded fall" in the Shuttle drop panel plays it again.
  A saved fall can also be opened directly:
    tripods.html?review=reviews/<session>/fall_001.json
  Pick an older fall in the list first to review that one. Resume / Space
  continues the video. A live stream keeps detecting and saving falls while
  frozen.
- Calibrate: freeze, then click the named court landmarks (Skip the ones not
  in view, at least 4), Finish. Saved as calib_cam<N>.json and loaded again
  next time. Until then the tripod model (same as camera_link.py) is used.
  The cyan court overlay shows whether the camera model fits the video.
- One camera gives exact landing spots (pixel -> floor). Flight heights are
  estimated assuming the shuttle falls in a vertical plane through the
  landing spot: --plane view (facing the camera, default) or --plane length
  (along the court length). Near-vertical falls come out accurately.
