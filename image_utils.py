"""Image helpers for the Now Playing background feature: cover-fit resize
and low-opacity blending, used since Tk canvases don't support real alpha
compositing against arbitrary content underneath."""

from PIL import Image


def fit_image_cover(image, target_size):
    """Resize+crop `image` (a PIL Image) to exactly fill target_size,
    cropping any excess from the center (like CSS `background-size:
    cover`), so it fills a box of that size without distortion."""
    target_w, target_h = target_size
    src_w, src_h = image.size
    if src_w <= 0 or src_h <= 0:
        return Image.new("RGB", target_size, color="#3a3a3a")
    scale = max(target_w / src_w, target_h / src_h)
    new_w = max(1, round(src_w * scale))
    new_h = max(1, round(src_h * scale))
    resized = image.resize((new_w, new_h), Image.LANCZOS)
    left = (new_w - target_w) // 2
    top = (new_h - target_h) // 2
    return resized.crop((left, top, left + target_w, top + target_h))


def fit_image_contain(image, target_size, fill_color=(240, 240, 240)):
    """Resize `image` (a PIL Image) to fit entirely within target_size,
    preserving aspect ratio (like CSS `background-size: contain`), so the
    whole image is always visible with no cropping. Any leftover space is
    padded with `fill_color`."""
    target_w, target_h = target_size
    src_w, src_h = image.size
    if src_w <= 0 or src_h <= 0:
        return Image.new("RGB", target_size, color=fill_color)
    scale = min(target_w / src_w, target_h / src_h)
    new_w = max(1, round(src_w * scale))
    new_h = max(1, round(src_h * scale))
    resized = image.resize((new_w, new_h), Image.LANCZOS)
    canvas = Image.new("RGB", target_size, color=fill_color)
    left = (target_w - new_w) // 2
    top = (target_h - new_h) // 2
    canvas.paste(resized, (left, top))
    return canvas


def apply_low_opacity(image, opacity=0.25, base_color=(240, 240, 240)):
    """Blend `image` (a PIL Image) at `opacity` (0-1) over a solid
    `base_color`, simulating a translucent/washed-out background image
    since Tk canvases don't support real alpha compositing with whatever
    is drawn underneath."""
    image = image.convert("RGBA")
    r, g, b, a = image.split()
    a = a.point(lambda p: int(p * opacity))
    image = Image.merge("RGBA", (r, g, b, a))
    background = Image.new("RGBA", image.size, base_color + (255,))
    composed = Image.alpha_composite(background, image)
    return composed.convert("RGB")
