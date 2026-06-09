"""Generate eye_crypt.ico — multi-resolution Windows icon for the bot."""
from PIL import Image, ImageDraw, ImageFont

SIZES = [16, 24, 32, 48, 64, 128, 256]


def build():
    imgs = []
    for sz in SIZES:
        img = Image.new("RGBA", (sz, sz), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        r = int(sz * 0.22)
        # base violet rounded square
        d.rounded_rectangle((1, 1, sz - 2, sz - 2), radius=r, fill=(139, 92, 246, 255))
        # fuchsia bottom overlay (fake gradient)
        overlay = Image.new("RGBA", (sz, sz), (0, 0, 0, 0))
        ImageDraw.Draw(overlay).rounded_rectangle(
            (1, sz // 2, sz - 2, sz - 2), radius=r, fill=(217, 70, 239, 110),
        )
        img = Image.alpha_composite(img, overlay)
        # "E" letter
        try:
            font = ImageFont.truetype("arialbd.ttf", int(sz * 0.62))
        except Exception:
            font = ImageFont.load_default()
        bbox = ImageDraw.Draw(img).textbbox((0, 0), "E", font=font)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        pos = ((sz - tw) // 2 - bbox[0], (sz - th) // 2 - bbox[1] - int(sz * 0.04))
        ImageDraw.Draw(img).text(pos, "E", fill=(255, 255, 255, 255), font=font)
        imgs.append(img)
    imgs[0].save(
        "eye_crypt.ico",
        format="ICO",
        sizes=[(s, s) for s in SIZES],
        append_images=imgs[1:],
    )
    print("Wrote eye_crypt.ico")


if __name__ == "__main__":
    build()
