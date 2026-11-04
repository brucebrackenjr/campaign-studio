"""Purpose-specific text bounds, counted as Unicode code points without trimming."""

LIMITS = {
    "campaign_name": 25,
    "brand_name": 20,
    "product_name": 80,
    "headline": 120,
    "cta": 60,
    "audience": 500,
    "direction": 1000,
    "description": 1000,
    "origin_label": 200,
    "rights": 1000,
    "phrase": 256,
    "file_reference": 4096,
    "replay_model": 200,
    "replay_history": 2000,
}
LABELS = {
    "campaign_name": "Campaign name",
    "brand_name": "Brand name",
    "product_name": "Product name",
    "headline": "Headline",
    "cta": "Call to action",
    "audience": "Audience",
    "direction": "Creative direction",
    "description": "Description",
    "origin_label": "Origin label",
    "rights": "Usage rights",
    "phrase": "Prohibited phrase",
    "file_reference": "File reference",
    "replay_model": "Replay model",
    "replay_history": "Replay history",
}
# IDs are shared with the existing form and pending-draft controls.
FIELDS = {
    "new-name": "campaign_name",
    "rename-name": "campaign_name",
    "campaign-name": "campaign_name",
    "bf-campaignName": "campaign_name",
    "new-brand": "brand_name",
    "brand-name": "brand_name",
    "bf-brandName": "brand_name",
    "new-audience": "audience",
    "audience": "audience",
    "bf-audience": "audience",
    "scene": "direction",
    "bf-direction": "direction",
    "copy-origin": "origin_label",
    "bf-copyOrigin": "origin_label",
    "copy-rights": "rights",
    "bf-copyRights": "rights",
    "copy-headline": "headline",
    "bf-message": "headline",
    "mf-word-message": "headline",
    "copy-cta": "cta",
    "bf-cta": "cta",
    "mf-word-cta": "cta",
    "product-name": "product_name",
    "product-description": "description",
    "product-origin": "origin_label",
    "product-rights": "rights",
    "image-description": "description",
    "mw-rule-phrase": "phrase",
}


def browser_contract():
    return {"limits": LIMITS, "labels": LABELS, "fields": FIELDS, "counting": "unicode_code_points"}


def brief_text_fields(brief):
    """Visit declared fields only; an incomplete draft need not be a valid Brief."""

    def member(value, path, names):
        if isinstance(value, dict):
            for field, purpose in names.items():
                yield path + [field], purpose, value.get(field)

    def origin(value, path):
        yield from member(value, path, {"label": "origin_label", "rights": "rights"})

    def wording(value, path):
        yield from member(value, path, {"message": "headline", "cta": "cta"})

    if not isinstance(brief, dict):
        return
    yield from member(
        brief,
        [],
        {"name": "campaign_name", "audience": "audience", "creative_direction": "direction"},
    )
    brand = brief.get("brand")
    yield from member(brand, ["brand"], {"name": "brand_name", "logo_path": "file_reference"})
    if isinstance(brand, dict):
        yield from origin(brand.get("logo_provenance"), ["brand", "logo_provenance"])
    yield from origin(brief.get("copy_provenance"), ["copy_provenance"])
    markets = brief.get("markets")
    for market, content in markets.items() if isinstance(markets, dict) else []:
        if not isinstance(content, dict):
            continue
        yield from wording(content.get("copy"), ["markets", market, "copy"])
        rules = content.get("phrase_rules")
        for index, rule in enumerate(rules if isinstance(rules, list) else []):
            yield from member(
                rule, ["markets", market, "phrase_rules", index], {"phrase": "phrase"}
            )
    products = brief.get("products")
    for index, product in enumerate(products if isinstance(products, list) else []):
        path = ["products", index]
        yield from member(product, path, {"name": "product_name", "description": "description"})
        if not isinstance(product, dict):
            continue
        source = product.get("source")
        yield from member(
            source, path + ["source"], {"path": "file_reference", "description": "description"}
        )
        if isinstance(source, dict):
            yield from origin(source.get("provenance"), path + ["source", "provenance"])
        overrides = product.get("copy_overrides")
        for market, content in overrides.items() if isinstance(overrides, dict) else []:
            yield from wording(content, path + ["copy_overrides", market])
    replay = brief.get("replay")
    yield from member(replay, ["replay"], {"history": "replay_history"})
    sources = replay.get("sources") if isinstance(replay, dict) else None
    for identity, source in sources.items() if isinstance(sources, dict) else []:
        path = ["replay", "sources", identity]
        yield from member(source, path, {"path": "file_reference", "model": "replay_model"})
        if isinstance(source, dict):
            yield from origin(source.get("provenance"), path + ["provenance"])


def authored_length_issues(authored):
    from campaign_studio.validation import field_issue

    fields = list(brief_text_fields(authored.get("brief")))
    forms = authored.get("form_drafts", {}).get("fields", {})
    reviews = authored.get("review_drafts", {})
    values = list(forms.values()) if isinstance(forms, dict) else []
    if isinstance(reviews, dict):
        values.extend(v.get("values") for v in reviews.values() if isinstance(v, dict))
    for form in values:
        if isinstance(form, dict):
            fields.extend(([], FIELDS[key], value) for key, value in form.items() if key in FIELDS)
    issues = []
    for path, purpose, value in fields:
        limit = LIMITS[purpose]
        if isinstance(value, str) and len(value) > limit:
            message = f"Use no more than {limit:,} characters ({len(value):,} entered)."
            issue = field_issue(path, "text_too_long", message)
            if not path:
                issue["label"] = LABELS[purpose]
            issues.append(issue)
    return issues
