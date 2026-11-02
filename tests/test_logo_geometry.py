"""Actual logo/text pixels, proportional bounds, and default layout."""

import pytest
from PIL import Image, ImageChops, ImageDraw
from pydantic import ValidationError

from campaign_studio.brief import FORMATS, Brief, Copy, Framing, LogoPlacement
from campaign_studio.imaging import (
    ImageProblem,
    compose,
    placed_logo_bounds,
    save_srgb,
    wording_mask,
)
from campaign_studio.planning import Output, make_plan
from campaign_studio.rendering import inspect_png
from campaign_studio.sample import write_sample
from campaign_studio.storage import Storage


def output_for(ratio, framing):
    width, height = FORMATS[ratio]
    return Output(
        id=f"product-1/en-US/{ratio.replace(':', 'x')}",
        product_id="product-1",
        market="en-US",
        ratio=ratio,
        width=width,
        height=height,
        copy=Copy(message="Exact supplied copy."),
        framing=framing,
    )


@pytest.mark.parametrize("ratio", list(FORMATS))
def test_intentional_copy_over_logo_saves_exact_text_and_visible_artwork(tmp_path, ratio):
    framing = Framing(logo_placement=LogoPlacement(left=0.04, top=0.02, width=0.85))
    output = output_for(ratio, framing)
    with (
        Image.new("RGB", (1920, 1920), "#123456") as scene,
        Image.new("RGBA", (500, 180), "#B62137") as logo,
    ):
        image = compose(scene, logo, output.text, ratio, "#FFFFFF", framing=framing)
        target = tmp_path / "output.png"
        save_srgb(image, target)
        checks = {f.rule: f for f in inspect_png(target, output, logo, "#FFFFFF")}
        assert all(f.severity == "pass" for f in checks.values())
        # Actual opaque lettering remains exact even where the larger red logo sits.
        with wording_mask(output.text, ratio, framing=framing) as text:
            mask = text.point(lambda v: 255 if v == 255 else 0)
            difference = ImageChops.difference(image, Image.new("RGB", image.size, "white"))
            assert Image.composite(difference, Image.new("RGB", image.size), mask).getbbox() is None
        assert image.getpixel((round(output.width * 0.1), round(output.height * 0.025))) == (
            182,
            33,
            55,
        )
        # Corrupting exposed artwork remains a blocking identity failure.
        ImageDraw.Draw(image).rectangle(
            (
                round(output.width * 0.1),
                round(output.height * 0.025),
                round(output.width * 0.1) + 2,
                round(output.height * 0.025) + 2,
            ),
            fill="black",
        )
        save_srgb(image, target)
        assert (
            next(
                f for f in inspect_png(target, output, logo, "#FFFFFF") if f.rule == "logo_pixels"
            ).severity
            == "blocking"
        )
        image.close()


def test_fully_covered_logo_has_no_reference_result_and_wrong_copy_color_still_blocks(tmp_path):
    framing = Framing(logo_placement=LogoPlacement(left=0, top=0, width=1))
    output = output_for("1:1", framing)
    with wording_mask(output.text, output.ratio, framing=framing) as text:
        # A real opaque logo whose pixels are all covered by the printed lettering.
        logo = Image.new("RGBA", FORMATS["1:1"], (182, 33, 55, 0))
        logo.putalpha(text.point(lambda v: 255 if v == 255 else 0))
    with logo, Image.new("RGB", (1920, 1920), "#123456") as scene:
        image = compose(scene, logo, output.text, output.ratio, "#FFFFFF", framing=framing)
        target = tmp_path / "covered.png"
        save_srgb(image, target)
        checks = {f.rule: f for f in inspect_png(target, output, logo, "#FFFFFF")}
        assert "logo_pixels" not in checks
        assert checks["controlled_text_color"].severity == "pass"
        assert not any(f.severity == "blocking" for f in checks.values())
        image.close()
        wrong = compose(scene, logo, output.text, output.ratio, "#000000", framing=framing)
        save_srgb(wrong, target)
        wrong.close()
        assert (
            next(
                f
                for f in inspect_png(target, output, logo, "#FFFFFF")
                if f.rule == "controlled_text_color"
            ).severity
            == "blocking"
        )


def test_logo_bounds_are_proportional_and_reject_outside_or_nonfinite_values():
    assert placed_logo_bounds(
        (400, 200), (1080, 1920), LogoPlacement(left=0.25, top=0.5, width=0.5)
    ) == (270, 960, 810, 1230)
    assert placed_logo_bounds((200, 200), (1080, 1080), LogoPlacement(left=0, top=0, width=1)) == (
        0,
        0,
        1080,
        1080,
    )
    with pytest.raises(ImageProblem, match="whole logo"):
        placed_logo_bounds((100, 400), (1920, 1080), LogoPlacement(left=0, top=0, width=0.5))
    for value in [0, -1, float("inf"), float("nan")]:
        with pytest.raises(ValidationError):
            LogoPlacement(left=0, top=0, width=value)


def test_plan_blocks_outside_logo_but_not_intentional_text_overlap(tmp_path):
    storage = Storage(tmp_path / "runtime")
    _, brief = write_sample(storage, 2, True)
    value = brief.model_dump(mode="json")
    value["products"][0]["framing"] = {
        "en-US": {"1:1": {"logo_placement": {"left": 0.02, "top": 0.02, "width": 0.9}}}
    }
    assert not make_plan(Brief.model_validate(value), storage).blocked
    value["products"][0]["framing"]["en-US"]["1:1"]["logo_placement"]["left"] = 0.5
    plan = make_plan(Brief.model_validate(value), storage)
    finding = next(f for f in plan.findings if f.rule == "logo_bounds")
    assert finding.severity == "blocking" and finding.context.product_id == "product-1"
    assert finding.context.fields[0].path[-1] == "logo_placement"
