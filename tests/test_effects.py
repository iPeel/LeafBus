"""Effects engine against the 14-panel layout from a real assembly."""
import pytest

import effects as fx
import geometry
import mpshim

LAY = geometry.layout(bytes.fromhex(mpshim.FOURTEEN))


def ctx(**k):
    return fx.Ctx(14, LAY['positions'], LAY['links'], **k)


def run(name, params=None, ms=15000, step=20, touch_at=None):
    e = fx.Engine(ctx())
    e.set(name, params or {}, 0)
    frames = []
    for t in range(0, ms, step):
        if touch_at is not None and t == touch_at:
            e.touch(3, t)
        f = e.tick(t)
        if f is not None:
            frames.append((t, f))
    return e, frames


@pytest.mark.parametrize('name', [n for n, _ in fx.EFFECTS])
def test_every_effect_makes_valid_frames(name):
    e, frames = run(name, touch_at=4000)
    assert frames, 'no frame at all'
    assert frames[0][0] == 0, 'first frame must come at once'
    first = frames[0][1]
    assert all(c is not None for c in first), 'first frame must paint every panel'
    for _, f in frames:
        assert len(f) == 14
        for c in f:
            assert c is None or (len(c) == 5 and all(0 <= v <= 255 for v in c))
            if c is not None:
                assert c[4] >= 1


def test_first_frame_fades_in():
    _, frames = run('solid')
    assert frames[0][1][0][4] == 5


def test_static_effect_is_sent_once_then_refreshed():
    _, frames = run('solid', ms=12000)
    times = [t for t, _ in frames]
    assert times[0] == 0
    assert len(times) == 3  # 0, ~5 s, ~10 s
    assert all(b - a >= fx.REFRESH_MS for a, b in zip(times, times[1:]))


def test_identical_frames_are_not_pushed():
    _, frames = run('flow', {'speed': 0}, ms=3000)
    assert len(frames) == 1


def test_disabled_panels_stay_dark():
    e = fx.Engine(ctx(disabled=[2, 5]))
    e.set('solid', {'color': '#ff0000'}, 0)
    f = e.tick(0)
    assert f[2][:4] == (0, 0, 0, 0) and f[5][:4] == (0, 0, 0, 0)
    assert f[0][:4] == (255, 0, 0, 0)


def test_params_are_cleaned():
    p = fx.clean(fx.Flow, {'speed': 999, 'direction': 'sideways', 'reverse': 'true',
                           'colors': ['#f00', 'nonsense', [0, 255, 0]]})
    assert p['speed'] == 100
    assert p['direction'] == 'horizontal'
    assert p['reverse'] is True
    assert p['colors'] == ['#ff0000', '#000000', '#00ff00']
    assert p['scale'] == 100


def test_scene_pads_to_panel_count():
    p = fx.clean(fx.Scene, {'colors': ['#112233']}, n=4)
    assert p['colors'] == ['#112233'] * 4


def test_color_parsing_keeps_white():
    assert fx.parse_color('#10203040') == (16, 32, 48, 64)
    assert fx.color_str((16, 32, 48, 64)) == '#10203040'
    assert fx.color_str((16, 32, 48, 0)) == '#102030'


def test_unknown_effect_rejected():
    with pytest.raises(ValueError):
        fx.Engine(ctx()).set('disco', {}, 0)


def test_ctx_features_are_normalised():
    c = ctx()
    for arr in (c.x, c.y, c.ix, c.iy, c.rad, c.ang, c.idx):
        assert min(arr) >= 0 and max(arr) <= 1.0 + 1e-9
    assert min(c.x) == 0 and max(c.x) == 1


def test_rotation_swaps_axes():
    a, b = ctx(), ctx(rotation=90)
    # after a quarter turn, left-right order becomes bottom-top order
    for i in range(14):
        assert abs(a.x[i] - b.y[i]) < 1e-6


def test_neighbours_include_ring_contacts():
    c = ctx()
    for a, b in LAY['links']:
        assert b in c.adj[a] and a in c.adj[b]
    # 2 and 11 touch without a bus link between them
    assert 11 in c.adj[2]


def test_hops_breadth_first():
    h = ctx().hops(0)
    assert h[0] == 0
    for j in ctx().adj[0]:
        assert h[j] == 1


def test_ripple_reacts_to_touch():
    e = fx.Engine(ctx())
    e.set('ripple', {'auto': 0, 'base': '#000000', 'colors': ['#ffffff']}, 0)
    e.tick(0)
    assert e.tick(500) is None           # touch only: nothing moves
    e.touch(3, 1000)
    f = e.tick(1000)
    assert f is not None and f[3][:3] == (255, 255, 255)


def test_resume_repaints_static_frame():
    e = fx.Engine(ctx())
    e.set('solid', {}, 0)
    e.tick(0)
    assert e.tick(100) is None
    e.resume(200)
    assert e.tick(200) is not None


def test_catalogue_describes_params():
    cat = fx.catalogue()
    names = [c['name'] for c in cat]
    assert names[0] == 'solid' and 'ripple' in names
    flow = next(c for c in cat if c['name'] == 'flow')
    speed = next(p for p in flow['params'] if p['key'] == 'speed')
    assert speed['min'] == 0 and speed['max'] == 100
