"""video-cook stage scripts.

Deterministic pipeline stages physically vendored into the cook package
(2026-10 reflux): the dub stage scripts live in ``stages/dub/``, the
subtitle stage scripts in ``stages/subtitle/``. cook resolves these paths
relative to its own installed location, so ``pip install -U`` upgrades
everything at once and installed-skill copies can no longer drift out of
sync with the CLI that drives them.
"""
