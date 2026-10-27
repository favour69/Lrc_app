from PIL import Image, ImageDraw, ImageOps

from pathlib import Path

base = Path(__file__).resolve().parent.parent / 'static'
base.mkdir(exist_ok=True)

for size, name in [(192, 'icon-192.png'), (512, 'icon-512.png'), (512, 'icon-maskable-512.png')]:
    img = Image.new('RGBA', (size, size), (18, 32, 48, 255))
    draw = ImageDraw.Draw(img)

    # Soft rounded rectangle background
    pad = size // 8
    shape = [pad, pad, size - pad, size - pad]
    draw.rounded_rectangle(shape, radius=size // 6, fill=(54, 93, 151, 255))

    # Inner glow accent circle
    accent = (145, 185, 255, 255)
    draw.ellipse((size // 4, size // 4, size - size // 4, size - size // 4), fill=accent)

    # Overlay simple music note icon
    note_x = size // 2
    note_y = size // 2
    draw.ellipse((note_x - size // 10, note_y - size // 5, note_x + size // 10, note_y + size // 3), fill=(255, 255, 255, 200))
    draw.rectangle((note_x + size // 22, note_y - size // 5, note_x + size // 20, note_y + size // 2), fill=(255, 255, 255, 200))
    draw.arc((note_x - size // 9, note_y - size // 6, note_x + size // 6, note_y + size // 4), 180, 360, fill=(255, 255, 255, 200), width=max(4, size // 20))

    if name == 'icon-maskable-512.png':
        img = ImageOps.pad(img, (512, 512), color=(18, 32, 48, 255), centering=(0.5, 0.5))

    img.save(base / name)

print('Created icons:', *sorted(p.name for p in base.glob('icon*.png')), sep='\n')
