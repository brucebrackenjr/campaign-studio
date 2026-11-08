"""Local decoding, color handling, measurable layout, and deterministic composition."""

import io
import struct
import warnings
from dataclasses import dataclass
from functools import lru_cache
from importlib.resources import files
from pathlib import Path

from fontTools.ttLib import TTFont, TTLibError
from PIL import (
    Image,
    ImageCms,
    ImageDraw,
    ImageFilter,
    ImageFont,
    ImageOps,
    UnidentifiedImageError,
)

from campaign_studio.brief import FORMATS, Box, Copy, Framing, LogoPlacement, Ratio


class ImageProblem(ValueError):
    pass


class ColorProfileProblem(ImageProblem):
    """Decoded pixels need a usable color profile or an sRGB replacement."""


class EmptyLogoProblem(ImageProblem):
    """A fully transparent asset cannot provide visible logo artwork."""


def require_visible_logo(image):
    if "A" in image.getbands() and image.getchannel("A").getbbox() is None:
        raise EmptyLogoProblem("This logo has no visible artwork. Choose a visible PNG or JPEG.")


def image_suffix(payload):
    """Name a verified original by its encoded type, not the supplied filename."""
    if payload.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if payload.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    raise ImageProblem("Only JPEG and PNG assets are supported.")


class FontProblem(ImageProblem):
    def __init__(self):
        super().__init__(
            "The bundled composition font is missing or unreadable. "
            "Reinstall Motif – Campaign Studio to restore it, then try again. "
            "Your campaign settings and source images are unchanged."
        )


def font_path(font_id="noto_sans") -> str:
    resources = {
        "noto_sans": "resources/fonts/NotoSans-Regular.ttf",
        "barlow_condensed": "static/motif/assets/BarlowCondensed-SemiBold.ttf",
    }
    return str(files("campaign_studio").joinpath(resources[font_id]))


def _font_characters(font_id="noto_sans") -> frozenset[int]:
    try:
        with TTFont(font_path() if font_id == "noto_sans" else font_path(font_id)) as font:
            characters = font.getBestCmap()
            if not characters:
                raise FontProblem()
            return frozenset(characters)
    except (OSError, TTLibError, KeyError, struct.error) as error:
        raise FontProblem() from error


@lru_cache
def supported_characters(font_id="noto_sans") -> frozenset[int]:
    return _font_characters(font_id)


def load_font(size: int, font_id="noto_sans") -> ImageFont.FreeTypeFont:
    try:
        return ImageFont.truetype(
            font_path() if font_id == "noto_sans" else font_path(font_id), size
        )
    except OSError as error:
        raise FontProblem() from error


def require_font() -> None:
    # Recheck the actual resource even if glyph coverage was cached before damage.
    _font_characters()
    load_font(16)


def load_image(path: Path | io.BytesIO) -> tuple[Image.Image, str]:
    """Return an oriented sRGB copy, leaving the named original untouched."""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(path) as source:
                if source.format not in {"JPEG", "PNG"}:
                    raise ImageProblem("Only JPEG and PNG assets are supported.")
                source.load()
                kind = source.format
                profile = source.info.get("icc_profile")
                image = ImageOps.exif_transpose(source).copy()
        alpha = "A" in image.getbands() or "transparency" in image.info
        if profile:
            try:
                incoming = ImageCms.ImageCmsProfile(io.BytesIO(profile))
                image = ImageCms.profileToProfile(
                    image,
                    incoming,
                    ImageCms.createProfile("sRGB"),
                    outputMode="RGBA" if alpha else "RGB",
                )
            except (OSError, ValueError, ImageCms.PyCMSError) as error:
                raise ColorProfileProblem(
                    "This image needs a supported color profile or an sRGB replacement."
                ) from error
        elif image.mode == "CMYK":
            raise ColorProfileProblem(
                "Untagged CMYK needs a supported color profile or an sRGB replacement."
            )
        image = image.convert("RGBA" if alpha else "RGB")
        return image, kind
    except (OSError, SyntaxError, UnidentifiedImageError, ImageCms.PyCMSError, ValueError) as error:
        if isinstance(error, ImageProblem):
            raise
        raise ImageProblem("The image could not be decoded. Choose a valid PNG or JPEG.") from error
    except (Image.DecompressionBombWarning, Image.DecompressionBombError) as error:
        raise ImageProblem("The image exceeds Pillow's decompression safety guard.") from error


def crop_box(
    size: tuple[int, int], ratio: Ratio, framing: Framing | None = None
) -> tuple[float, float, float, float]:
    width, height = size
    target_width, target_height = FORMATS[ratio]
    framing = framing or Framing()
    scale = max(target_width / width, target_height / height) * framing.zoom
    cropped_width, cropped_height = target_width / scale, target_height / scale
    room_x, room_y = max(0, width - cropped_width), max(0, height - cropped_height)
    left = room_x * (framing.focal_x if room_x > 0.5 else 0.5)
    top = room_y * (framing.focal_y if room_y > 0.5 else 0.5)
    return left, top, left + cropped_width, top + cropped_height


def protects_subject(
    size: tuple[int, int], crop: tuple[float, float, float, float], box: Box
) -> bool:
    width, height = size
    return (
        crop[0] - 0.5 <= box.left * width
        and crop[1] - 0.5 <= box.top * height
        and crop[2] + 0.5 >= box.right * width
        and crop[3] + 0.5 >= box.bottom * height
    )


@dataclass(frozen=True)
class TextLine:
    text: str
    x: int
    y: int
    size: int
    font_id: str = "noto_sans"
    color: str | None = None


def _text_lines(
    text: str,
    size: int,
    available: int,
    x: int,
    y: int,
    font_id="noto_sans",
    color=None,
    *,
    measure_only=False,
):
    unsupported = {c for c in text if c != "\n" and ord(c) not in supported_characters(font_id)}
    if unsupported and not measure_only:
        raise ImageProblem("The bundled font cannot render every supplied character.")
    font = load_font(size, font_id)
    ascent, descent = font.getmetrics()
    step = ascent + descent + round(size * 0.15)
    lines = []
    for paragraph in text.split("\n"):
        current = ""
        for index, word in enumerate(paragraph.split(" ")):
            addition = word if index == 0 else " " + word
            if font.getlength(current + addition) <= available:
                current += addition
            elif current:
                lines.append(TextLine(current, x, y, size, font_id, color))
                y += step
                current = word
            elif not measure_only:
                raise ImageProblem("An unbroken copy segment is too wide for the output.")
            else:
                current = word
            if not measure_only and font.getlength(current) > available:
                raise ImageProblem("An unbroken copy segment is too wide for the output.")
        lines.append(TextLine(current, x, y, size, font_id, color))
        y += step
    return lines, y + round(size * 0.2)


def _glyph_bounds(lines):
    bounds = []
    for line in lines:
        left, top, right, bottom = load_font(line.size, line.font_id).getbbox(
            line.text, anchor="lt"
        )
        if right > left and bottom > top:
            bounds.append((line.x + left, line.y + top, line.x + right, line.y + bottom))
    if not bounds:
        return [0, 0, 0, 0]
    return [
        min(b[0] for b in bounds),
        min(b[1] for b in bounds),
        max(b[2] for b in bounds),
        max(b[3] for b in bounds),
    ]


def text_blocks(copy: Copy, ratio: Ratio, framing=None, *, validate=True):
    """One layout supplies composition, exact masks, and editor geometry.

    The wrapping column is independent of position. Only actual glyph bounds
    determine containment; an invisible column or UI selection frame must not
    prevent moving a short line closer to the canvas edge.
    """
    width, height = FORMATS[ratio]
    margin = round(min(width, height) * 0.06)
    available = width - 2 * margin
    placements = framing.text_placements if framing else {}
    y = margin
    blocks = []
    for key, text, size in (
        ("message", copy.message, 60),
        ("cta", copy.cta, 38),
    ):
        if text is None:
            continue
        # Preserve the old positions of blocks that have not been arranged. An
        # arranged block is validated with its chosen font/size below, not the
        # unused default font/size used to locate its untouched neighbors.
        lines, y = _text_lines(
            text, size, available, margin, y, measure_only=not validate or key in placements
        )
        blocks.append(
            {
                "id": key,
                "text": text,
                "lines": lines,
                "font_size": size,
                "placement": {
                    "left": margin / width,
                    "top": lines[0].y / height,
                    "width": available / width,
                    "font_size": size,
                    "font_id": "noto_sans",
                    "color": None,
                },
            }
        )
    if validate and not placements and y > height * 0.36:
        raise ImageProblem(
            "Supplied copy exceeds the current layout; shorten it or revise the layout."
        )
    if framing and framing.copy_placement == "bottom":
        shift = height - margin - round(height * 0.12) - y
        for block in blocks:
            block["lines"] = [
                TextLine(line.text, line.x, line.y + shift, line.size, line.font_id, line.color)
                for line in block["lines"]
            ]
            block["placement"]["top"] = block["lines"][0].y / height
    for block in blocks:
        if placement := placements.get(block["id"]):
            block["lines"], _ = _text_lines(
                block["text"],
                placement.font_size,
                max(1, round(width * placement.width)),
                round(width * placement.left),
                round(height * placement.top),
                placement.font_id,
                placement.color,
                measure_only=not validate,
            )
            block["font_size"] = placement.font_size
            block["placement"] = placement.model_dump()
        block["font_id"] = block["lines"][0].font_id
        block["color"] = block["lines"][0].color
        block["bounds"] = _glyph_bounds(block["lines"])
        if placements and validate:
            left, top, right, bottom = block["bounds"]
            if left < 0 or top < 0 or right > width or bottom > height:
                label = {
                    "message": "Headline",
                    "cta": "Call to action",
                }[block["id"]]
                raise ImageProblem(
                    f"{label} extends beyond the canvas. "
                    "Move or resize it to keep every character visible."
                )
    return blocks


def text_layout(copy: Copy, ratio: Ratio, framing: Framing | None = None) -> list[TextLine]:
    return [line for block in text_blocks(copy, ratio, framing) for line in block["lines"]]


def placed_logo_bounds(
    logo_size: tuple[int, int], size: tuple[int, int], placement: LogoPlacement
) -> tuple[int, int, int, int]:
    """Resolve one proportional, canvas-contained output rectangle."""
    width, height = size
    scaled_width = max(1, round(width * placement.width))
    scaled_height = max(1, round(width * placement.width * logo_size[1] / logo_size[0]))
    left, top = round(width * placement.left), round(height * placement.top)
    if left + scaled_width > width or top + scaled_height > height:
        raise ImageProblem("Keep the whole logo inside this image’s edges.")
    return left, top, left + scaled_width, top + scaled_height


def fit_logo_placement(logo_size, size, placement: LogoPlacement) -> LogoPlacement:
    """Fit a replacement proportionally at the existing top-left, before preview."""
    width, height = size
    left = min(round(width * placement.left), width - 1)
    top = min(round(height * placement.top), height - 1)
    fitted_width = min(
        placement.width,
        (width - left) / width,
        (height - top) * logo_size[0] / logo_size[1] / width,
    )
    return LogoPlacement(left=left / width, top=top / height, width=fitted_width)


def logo_placement(
    logo: Image.Image | None, size: tuple[int, int], placement: LogoPlacement | None = None
) -> tuple[Image.Image, tuple[int, int]]:
    width, height = size
    if placement is not None:
        left, top, right, bottom = placed_logo_bounds(logo.size, size, placement)
        return logo.convert("RGBA").resize(
            (right - left, bottom - top), Image.Resampling.LANCZOS
        ), (left, top)
    # Default placement fits the original artwork without enlarging it.
    result = logo.convert("RGBA")
    result.thumbnail((round(width * 0.15), round(height * 0.09)), Image.Resampling.LANCZOS)
    margin = round(min(size) * 0.06)
    return result, (width - margin - result.width, height - margin - result.height)


def wording_mask(copy: Copy, ratio: Ratio, framing=None) -> Image.Image:
    """The same raster coverage as the printed wording, including antialiased edges."""
    mask = Image.new("L", FORMATS[ratio])
    draw = ImageDraw.Draw(mask)
    for line in text_layout(copy, ratio, framing):
        draw.text(
            (line.x, line.y),
            line.text,
            font=load_font(line.size, line.font_id),
            fill=255,
            anchor="lt",
        )
    return mask


def product_shadow(output: Image.Image, product: Image.Image, position: tuple[int, int]) -> None:
    """Ground an opaque cutout with a soft shadow for the supported upper-left light.

    Use the visible base, excluding one-unit alpha residue and transparent padding.
    The product layer itself is never recolored, reshaped, or edited.
    """
    mask = product.getchannel("A").point(lambda alpha: 255 if alpha >= 2 else 0)
    bounds = mask.getbbox()
    if bounds is None:
        raise ImageProblem("The prepared product has no visible footprint.")
    left, top, right, bottom = bounds
    base_top = bottom - max(1, round((bottom - top) * 0.12))
    base = mask.crop((left, base_top, right, bottom)).getbbox()
    base_left, base_right = left + base[0], left + base[2]
    width = max(2, base_right - base_left)
    center = position[0] + (base_left + base_right) / 2
    ground = position[1] + bottom - 1
    for rectangle, opacity, blur in (
        # The broad cast shadow falls gently rightward from upper-left illumination.
        (
            (
                center - width * 0.45,
                ground - width * 0.04,
                center + width * 0.85,
                ground + width * 0.16,
            ),
            48,
            width * 0.045,
        ),
        # The tighter contact shadow joins the visible base directly to the surface.
        (
            (
                center - width * 0.46,
                ground - width * 0.045,
                center + width * 0.46,
                ground + width * 0.045,
            ),
            105,
            width * 0.018,
        ),
    ):
        alpha = Image.new("L", output.size)
        ImageDraw.Draw(alpha).ellipse(rectangle, fill=opacity)
        alpha = alpha.filter(ImageFilter.GaussianBlur(max(1, blur)))
        shadow = Image.new("RGBA", output.size)
        shadow.putalpha(alpha)
        output.alpha_composite(shadow)
        shadow.close()
        alpha.close()


def compose(
    scene: Image.Image,
    logo: Image.Image | None,
    copy: Copy,
    ratio: Ratio,
    color: str,
    product: Image.Image | None = None,
    framing: Framing | None = None,
) -> Image.Image:
    output = composition_base(scene, ratio, product, framing)
    # All compositions use one layer order: scene, logo, then printed wording.
    if logo is not None:
        artwork, position = logo_placement(
            logo, output.size, framing.logo_placement if framing else None
        )
        output.alpha_composite(artwork, position)
        artwork.close()
    if framing and framing.text_placements:
        composite_text_blocks(output, text_blocks(copy, ratio, framing), color)
    else:
        overlay = wording_overlay(copy, ratio, color, framing)
        output.alpha_composite(overlay)
        overlay.close()
    return output.convert("RGB")


def composition_base(scene, ratio, product=None, framing=None) -> Image.Image:
    """Exact crop, product, and shadow without a printed logo or typography."""
    size = FORMATS[ratio]
    crop = crop_box(scene.size, ratio, framing)
    if (
        framing
        and framing.subject_box
        and not protects_subject(scene.size, crop, framing.subject_box)
    ):
        raise ImageProblem(
            "This crop cuts protected subject detail. Move the focal point or revise the bounds."
        )
    output = scene.resize(size, Image.Resampling.LANCZOS, box=crop).convert("RGBA")
    if product is not None:
        cutout = product.convert("RGBA")
        cutout.thumbnail((round(size[0] * 0.55), round(size[1] * 0.5)), Image.Resampling.LANCZOS)
        position = ((size[0] - cutout.width) // 2, round(size[1] * 0.43))
        product_shadow(output, cutout, position)
        output.alpha_composite(cutout, position)
        cutout.close()
    return output


def text_block_overlay(lines, size, color):
    output = Image.new("RGBA", size)
    draw = ImageDraw.Draw(output)
    for line in lines:
        draw.text(
            (line.x, line.y),
            line.text,
            font=load_font(line.size, line.font_id),
            fill=line.color or color,
            anchor="lt",
        )
    return output


def composite_text_blocks(output, blocks, color):
    """Composite each exact editor layer in renderer order, rounding only once per layer."""
    for block in blocks:
        with text_block_overlay(block["lines"], output.size, color) as layer:
            output.alpha_composite(layer)


def wording_overlay(copy, ratio, color, framing=None):
    if framing and framing.text_placements:
        # Edited text uses the very same independently decoded block layers as
        # the editor. Alpha-composition preserves earlier pixels at soft edges.
        result = Image.new("RGBA", FORMATS[ratio])
        composite_text_blocks(result, text_blocks(copy, ratio, framing), color)
        return result
    return text_block_overlay(text_layout(copy, ratio, framing), FORMATS[ratio], color)


def creative_overlay(
    logo: Image.Image | None,
    copy: Copy,
    ratio: Ratio,
    color: str,
    framing: Framing | None = None,
) -> Image.Image:
    """The renderer's exact wording/logo layer, also used for temporary client motion."""
    size = FORMATS[ratio]
    output = Image.new("RGBA", size)
    if logo is not None:
        logo_image, position = logo_placement(
            logo, size, framing.logo_placement if framing else None
        )
        output.alpha_composite(logo_image, position)
        logo_image.close()
    if framing and framing.text_placements:
        composite_text_blocks(output, text_blocks(copy, ratio, framing), color)
    else:
        draw = ImageDraw.Draw(output)
        for line in text_layout(copy, ratio, framing):
            draw.text(
                (line.x, line.y),
                line.text,
                font=load_font(line.size, line.font_id),
                fill=line.color or color,
                anchor="lt",
            )
    return output


def save_srgb(image: Image.Image, path: Path, *, compress_level: int | None = None) -> None:
    profile = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    with image.copy() as sanitized:
        sanitized.info.clear()
        encoding = {} if compress_level is None else {"compress_level": compress_level}
        sanitized.save(path, format="PNG", icc_profile=profile, **encoding)
