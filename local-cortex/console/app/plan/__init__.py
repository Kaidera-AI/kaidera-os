"""The `plan` feature module — read surface over visual-plan `.mdx` files.

A visual plan is a structured MDX document (frontmatter + markdown + diagram/file-map/
annotated-code/wireframe blocks) authored with the `visual-plan` skill before code, for
human review. This module fronts those files (which live in the project tree under
`docs/plans/`) with two read-only routes; the SPA's `MdxPlanRenderer` renders the MDX.

`main.py` mounts the router additively (`app.include_router(plan.router)`).
"""

from app.plan.api import router

__all__ = ["router"]
