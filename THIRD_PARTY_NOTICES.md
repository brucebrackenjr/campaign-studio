# Third-party notices and example origins

## Included campaign imagery

Autumn Winds and Bridle & Bough are fictional demonstration brands. Their supplied product scenes and selected logos were prepared with built-in image generation outside Motif, then included as local input assets. They are not photographs of verified commercial products or evidence of an application provider request. Their brief provenance describes the supplied imagery, and the packaged campaign manifest binds the asset hashes.

The initial Autumn campaign composes 30 outputs locally. Bridle has two supplied product images and one deliberately missing concept; its initial plan remains unconfirmed. Setup makes no provider request. The included content does not establish a commercial-product affiliation, exclusive rights claim, or publication approval.

The geometric assets in `examples/synthetic-2` and `examples/synthetic-3` are locally authored engineering fixtures. The separate CLI replay fixture retains the fictional HOLD campaign: one AI-created supplied chalk scene, a locally authored wordmark, and one genuine OpenAI-generated brush-roll concept master. Its [README](src/campaign_studio/resources/replay/README.md) and brief retain the actual origin, historical model/date, and exact master hash. Replaying it makes no new image request. Credentials, raw provider responses, and private preparation records are not included.

## Fonts

The Motif interface bundles unmodified Barlow Condensed SemiBold and Noto Sans under the SIL Open Font License 1.1. Their original licenses are preserved beside the assets:

- [Barlow Condensed license](src/campaign_studio/static/motif/assets/Barlow-OFL.txt)
- [Interface Noto Sans license](src/campaign_studio/static/motif/assets/NotoSans-OFL.txt)

The compositor uses the packaged Barlow Condensed SemiBold face and the separate unmodified Noto Sans Regular font. The latter’s complete license and original copyright notice are in [resources/fonts/OFL.txt](src/campaign_studio/resources/fonts/OFL.txt). Its source is the [Noto font repository](https://github.com/notofonts/noto-fonts/blob/main/hinted/ttf/NotoSans/NotoSans-Regular.ttf), retrieved October 28, 2026. Font SHA-256: `b85c38ecea8a7cfb39c24e395a4007474fa5a4fc864f6ee33309eb4948d232d5`. No font modification is claimed. The interface Noto Sans file and the compositor’s Noto Sans Regular file are distinct assets.

## Dependencies and original code

Installed dependencies retain their own licenses and notices. See their distribution metadata and official projects: [FastAPI](https://github.com/fastapi/fastapi), [Uvicorn](https://github.com/encode/uvicorn), [Jinja](https://github.com/pallets/jinja), [Pydantic](https://github.com/pydantic/pydantic), [Pillow](https://github.com/python-pillow/Pillow), [fontTools](https://github.com/fonttools/fonttools), and [OpenAI Python](https://github.com/openai/openai-python). Transitive dependencies are pinned in `uv.lock` and included in the exported requirements.

No license grant for original project code is specified by these dependency or asset references.
