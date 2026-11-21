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


# -- "Match Player Colors to Background Image" palette generation ---------
def _hls_hex(h, l, s):
    """Hue/lightness/saturation (each 0-1, clamped) -> "#rrggbb"."""
    import colorsys
    r, g, b = colorsys.hls_to_rgb(h, max(0.0, min(1.0, l)), max(0.0, min(1.0, s)))
    return "#%02x%02x%02x" % (round(r * 255), round(g * 255), round(b * 255))


def _luminance(rgb):
    """Perceived brightness of an (r, g, b) color, 0 (black) to 1 (white)."""
    r, g, b = (c / 255 for c in rgb)
    return 0.299 * r + 0.587 * g + 0.114 * b


def palette_from_image(image):
    """Build a full theme palette (same keys as styles.THEMES entries) from
    the dominant and most vivid colors of `image` (a PIL Image), so the
    whole player can be recolored to match a background photo. Dark photos
    produce a dark palette, bright photos a light one."""
    import colorsys

    small = image.convert("RGB")
    small.thumbnail((160, 160))
    quantized = small.quantize(colors=8, method=Image.Quantize.MEDIANCUT)
    raw_palette = quantized.getpalette() or []
    counts = sorted(quantized.getcolors() or [], reverse=True)
    total = sum(count for count, _ in counts) or 1
    colors = []
    for count, index in counts:
        rgb = tuple(raw_palette[index * 3:index * 3 + 3])
        if len(rgb) == 3:
            colors.append((count / total, rgb))
    if not colors:
        colors = [(1.0, (128, 128, 128))]

    def hls(rgb):
        """(r, g, b) 0-255 -> (hue, lightness, saturation) 0-1."""
        return colorsys.rgb_to_hls(*(c / 255 for c in rgb))

    avg_lum = sum(share * _luminance(rgb) for share, rgb in colors)
    is_dark = avg_lum < 0.55

    # Base hue: the most common color in the image.
    base_h, _base_l, base_s = hls(colors[0][1])
    base_s = min(base_s, 0.45)

    # Accent: the most vivid color that still covers a decent chunk of
    # the image (so a single stray bright pixel cluster doesn't win).
    def vividness(item):
        """Score a (share, rgb) color: saturated, mid-lightness colors that
        cover more of the image score highest."""
        share, rgb = item
        h, l, s = hls(rgb)
        return s * (1 - abs(l - 0.5) * 1.2) * (0.35 + share)

    ranked = sorted(colors, key=vividness, reverse=True)
    acc_h, _acc_l, acc_s = hls(ranked[0][1])
    acc_s = max(acc_s, 0.45) if acc_s > 0.08 else acc_s
    # Second accent (queued tracks): the next vivid color with a
    # noticeably different hue, else the accent hue shifted.
    acc2_h, acc2_s = (acc_h + 0.5) % 1.0, max(acc_s * 0.6, 0.25)
    for _share, rgb in ranked[1:]:
        h, _l, s = hls(rgb)
        hue_gap = min(abs(h - acc_h), 1 - abs(h - acc_h))
        if s > 0.15 and hue_gap > 0.08:
            acc2_h, acc2_s = h, max(s, 0.3)
            break

    if is_dark:
        bg = _hls_hex(base_h, 0.13, base_s)
        field_bg = _hls_hex(base_h, 0.09, base_s)
        fg = _hls_hex(base_h, 0.88, min(base_s, 0.25))
        button_bg = _hls_hex(acc_h, 0.42, acc_s)
        button_fg = "#111111" if _luminance(_hex_rgb(button_bg)) > 0.5 else "#f5f5f5"
        trough_bg = _hls_hex(base_h, 0.22, base_s)
        select_bg = _hls_hex(acc_h, 0.55, acc_s)
        highlight_bg = _hls_hex(acc_h, 0.32, acc_s)
        queue_bg = _hls_hex(acc2_h, 0.26, acc2_s)
    else:
        bg = _hls_hex(base_h, 0.90, base_s)
        field_bg = _hls_hex(base_h, 0.96, base_s)
        fg = _hls_hex(base_h, 0.10, min(base_s, 0.3))
        button_bg = _hls_hex(acc_h, 0.72, acc_s)
        button_fg = "#111111" if _luminance(_hex_rgb(button_bg)) > 0.5 else "#f5f5f5"
        trough_bg = _hls_hex(base_h, 0.80, base_s)
        select_bg = _hls_hex(acc_h, 0.42, acc_s)
        highlight_bg = _hls_hex(acc_h, 0.78, acc_s)
        queue_bg = _hls_hex(acc2_h, 0.85, acc2_s)

    def text_on(color):
        """Near-black or near-white text, whichever reads better on `color`."""
        return "#111111" if _luminance(_hex_rgb(color)) > 0.5 else "#f5f5f5"

    return {
        "bg": bg,
        "fg": fg,
        "field_bg": field_bg,
        "field_fg": fg,
        "select_bg": select_bg,
        "select_fg": text_on(select_bg),
        "button_bg": button_bg,
        "button_fg": button_fg,
        "trough_bg": trough_bg,
        "highlight_bg": highlight_bg,
        "highlight_fg": text_on(highlight_bg),
        "queue_bg": queue_bg,
        "queue_fg": text_on(queue_bg),
    }


def _hex_rgb(color):
    """"#rrggbb" -> (r, g, b)."""
    color = color.lstrip("#")
    return tuple(int(color[i:i + 2], 16) for i in (0, 2, 4))
