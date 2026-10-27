"""Generate the PWA icons in static/ (run: python scripts/generate_icons.py, needs Pillow)."""
from pathlib import Path

from PIL import Image, ImageDraw

base = Path(__file__).resolve().parent.parent / 'static'
base.mkdir(exist_ok=True)

BG_TOP = (74, 111, 165)     # --accent
BG_BOTTOM = (42, 70, 111)   # --accent-strong
NOTE = (255, 255, 255)
SS = 4  # supersampling factor for smooth edges


def gradient(size):
    img = Image.new('RGB', (size, size))
    px = img.load()
    for y in range(size):
        t = y / (size - 1)
        color = tuple(round(BG_TOP[i] + (BG_BOTTOM[i] - BG_TOP[i]) * t) for i in range(3))
        for x in range(size):
            px[x, y] = color
    return img


def draw_note(draw, size, scale):
    """Eighth note centred in the canvas; `scale` shrinks it to stay inside the maskable safe zone."""
    s = size * scale
    cx = size / 2
    cy = size / 2

    head_rx, head_ry = 0.17 * s, 0.125 * s
    head_cx = cx - 0.10 * s
    head_cy = cy + 0.26 * s
    stem_w = 0.055 * s
    stem_x1 = head_cx + head_rx - stem_w
    stem_x2 = head_cx + head_rx
    stem_top = cy - 0.36 * s

    # note head
    draw.ellipse([head_cx - head_rx, head_cy - head_ry, head_cx + head_rx, head_cy + head_ry], fill=NOTE)
    # stem
    draw.rectangle([stem_x1, stem_top, stem_x2, head_cy], fill=NOTE)
    # flag
    draw.polygon(
        [
            (stem_x2, stem_top),
            (stem_x2 + 0.26 * s, stem_top + 0.20 * s),
            (stem_x2 + 0.20 * s, stem_top + 0.36 * s),
            (stem_x2 + 0.02 * s, stem_top + 0.22 * s),
            (stem_x2, stem_top + 0.26 * s),
        ],
        fill=NOTE,
    )


def make_icon(size, maskable):
    big = size * SS
    img = gradient(big).convert('RGBA')

    if not maskable:
        # rounded corners for the regular icon
        mask = Image.new('L', (big, big), 0)
        ImageDraw.Draw(mask).rounded_rectangle([0, 0, big - 1, big - 1], radius=big // 5, fill=255)
        rounded = Image.new('RGBA', (big, big), (0, 0, 0, 0))
        rounded.paste(img, (0, 0), mask)
        img = rounded

    draw_note(ImageDraw.Draw(img), big, 0.62 if maskable else 0.78)
    return img.resize((size, size), Image.LANCZOS)


for size, name, maskable in [
    (192, 'icon-192.png', False),
    (512, 'icon-512.png', False),
    (512, 'icon-maskable-512.png', True),
]:
    make_icon(size, maskable).save(base / name)

print('Created icons:', *sorted(p.name for p in base.glob('icon*.png')), sep='\n')
