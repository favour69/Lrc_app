from pathlib import Path

from PIL import Image, ImageDraw, ImageOps

base = Path(__file__).resolve().parent.parent / 'static'
base.mkdir(exist_ok=True)

for size, name in [(192, 'icon-192.png'), (512, 'icon-512.png'), (512, 'icon-maskable-512.png')]:
    img = Image.new('RGBA', (size, size), (18, 32, 48, 255))
    draw = ImageDraw.Draw(img)

    pad = size // 8
    draw.rounded_rectangle([pad, pad, size - pad, size - pad], radius=size // 6, fill=(54, 93, 151, 255))
    draw.ellipse([size // 4, size // 4, size - size // 4, size - size // 4], fill=(145, 185, 255, 255))

    cx = size // 2
    cy = size // 2
    draw.ellipse([cx - size // 10, cy - size // 5, cx + size // 10, cy + size // 3], fill=(255, 255, 255, 200))
    draw.rectangle([cx + size // 22, cy - size // 5, cx + size // 20, cy + size // 2], fill=(255, 255, 255, 200))
    draw.arc([cx - size // 9, cy - size // 6, cx + size // 6, cy + size // 4], 180, 360, fill=(255, 255, 255, 200), width=max(4, size // 20))

    if name == 'icon-maskable-512.png':
        img = ImageOps.pad(img, (512, 512), color=(18, 32, 48, 255), centering=(0.5, 0.5))

    img.save(base / name)

print('Created icons:', *sorted(p.name for p in base.glob('icon*.png')), sep='\n')
