"""Image helpers for the Now Playing background feature: cover-fit resize
and low-opacity blending, used since Tk canvases don't support real alpha
compositing against arbitrary content underneath. Also has
extract_palette_from_image, which derives a full app color theme
(matching styles.PALETTE_KEYS) from an album cover's dominant colors,
for the "Album Art Theme" dynamic-theming feature."""

import colorsys

from PIL import Image


def _rgb_to_hex(rgb):
    r, g, b = (max(0, min(255, round(c))) for c in rgb)
    return "#%02x%02x%02x" % (r, g, b)


def _hsv_hex(h, s, v):
    r, g, b = colorsys.hsv_to_rgb(h % 1.0, max(
        0.0, min(1.0, s)), max(0.0, min(1.0, v)))
    return _rgb_to_hex((r * 255, g * 255, b * 255))


def _dominant_hues(image, max_colors=5):
    """Return up to `max_colors` (hue, saturation) pairs for `image`'s
    most common sufficiently-saturated/visible colors, most-common
    first. Near-black/near-white/near-gray pixels are excluded since
    they make poor accent colors (album art commonly has large plain
    white/black borders or backgrounds that would otherwise dominate the
    result)."""
    small = image.convert("RGB").resize((48, 48), Image.BILINEAR)
    counts = small.getcolors(48 * 48) or []
    counts.sort(key=lambda item: item[0], reverse=True)

    seen_buckets = set()
    hues = []
    for _count, (r, g, b) in counts:
        h, s, v = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
        if s < 0.18 or v < 0.12 or v > 0.95:
            continue
        # Bucket similar hues together (coarse rounding) so we don't
        # return several near-identical shades of the same color as if
        # they were distinct accents.
        bucket = round(h * 24)
        if bucket in seen_buckets:
            continue
        seen_buckets.add(bucket)
        hues.append((h, s))
        if len(hues) >= max_colors:
            break
    return hues


def extract_palette_from_image(image):
    """Derive a full color palette (matching styles.PALETTE_KEYS) from
    `image` (a PIL Image, e.g. an album cover) -- a dark theme tinted by
    the art's own dominant hue(s), similar to "adaptive"/"canvas" themes
    in other media players. Always produces a DARK, readable theme
    (rather than trying to match the art's own overall brightness)
    since album art is wildly inconsistent in exposure/contrast and a
    literal light/dark match would frequently be unreadable.

    Falls back to a plain neutral-gray dark palette (no strong tint) if
    no sufficiently vivid dominant color could be found (e.g. a mostly
    black-and-white or monochrome cover)."""
    hues = _dominant_hues(image)
    # Fall back to a neutral blue (matching the app's own default dark
    # theme's accent color) rather than an arbitrary hue when no vivid
    # dominant color was found, so a grayscale/monochrome cover doesn't
    # end up tinted an unrelated color practically at random.
    primary_h, primary_s = hues[0] if hues else (0.58, 0.0)
    secondary_h, secondary_s = hues[1] if len(hues) > 1 else (
        (primary_h + 0.45) % 1.0, primary_s)

    bg_s = primary_s * 0.35
    accent_s = max(primary_s, 0.55)

    bg = _hsv_hex(primary_h, bg_s, 0.11)
    field_bg = _hsv_hex(primary_h, bg_s, 0.07)
    button_bg = _hsv_hex(primary_h, bg_s, 0.20)
    trough_bg = _hsv_hex(primary_h, bg_s, 0.08)

    fg = _hsv_hex(primary_h, min(0.08, primary_s * 0.15), 0.93)
    field_fg = fg
    button_fg = fg

    select_bg = _hsv_hex(primary_h, accent_s, 0.60)
    # White text reads fine on every accent lightness this function
    # produces (capped at v=0.60), so no separate contrast check needed.
    select_fg = "#ffffff"

    highlight_bg = _hsv_hex(primary_h, accent_s, 0.72)
    highlight_fg = "#1a1a1a"

    queue_bg = _hsv_hex(secondary_h, max(secondary_s, 0.4), 0.34)
    queue_fg = fg

    return {
        "bg": bg, "fg": fg,
        "field_bg": field_bg, "field_fg": field_fg,
        "select_bg": select_bg, "select_fg": select_fg,
        "button_bg": button_bg, "button_fg": button_fg,
        "trough_bg": trough_bg,
        "highlight_bg": highlight_bg, "highlight_fg": highlight_fg,
        "queue_bg": queue_bg, "queue_fg": queue_fg,
    }


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
