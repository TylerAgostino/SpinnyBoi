# pyright: basic
"""Shared construction of the repo's standard status embeds."""
from typing import Optional

import discord


def status_embed(
    title: str, description: Optional[str] = None, ok: bool = True
) -> discord.Embed:
    """The standard success(green)/failure(red) embed used across cogs."""
    return discord.Embed(
        title=title,
        description=description,
        color=discord.Color.green() if ok else discord.Color.red(),
    )
