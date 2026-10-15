"""Who may do what in an organisation (MAC-03), checked on the server for every action."""

ROLES = ("owner", "admin", "developer", "viewer")
EVERYONE = frozenset(ROLES)
MANAGERS = frozenset({"owner", "admin"})
BUILDERS = frozenset({"owner", "admin", "developer"})

ALLOWED = {
    "organisation.read": EVERYONE,
    "usage.read": EVERYONE,
    "audit.read": MANAGERS,
    "organisation.update": MANAGERS,
    "member.invite": MANAGERS,  # inviting an owner: owners only (invite_roles)
    "member.change_role": MANAGERS,
    "member.remove": MANAGERS,
    "key.personal": BUILDERS,
    "key.service": MANAGERS,
    "key.revoke_any": MANAGERS,
    "client.connect": BUILDERS,
    "client.revoke_any": MANAGERS,
    "access.request": frozenset({"owner"}),
    "access.end": frozenset({"owner"}),
    "organisation.delete": frozenset({"owner"}),
    "ownership.transfer": frozenset({"owner"}),
}


def can(role: str | None, action: str) -> bool:
    return role in ALLOWED[action]


def invite_roles(role: str) -> tuple[str, ...]:
    """The roles a member may grant by invitation."""
    if role == "owner":
        return ROLES
    if role == "admin":
        return ("admin", "developer", "viewer")
    return ()


def may_change(actor_role: str, actor_is_target: bool, old: str, new: str) -> bool:
    """Role changes: owners change any; admins move people among admin, developer and viewer, not
    themselves; nobody raises their own role."""
    if actor_is_target and ROLES.index(new) < ROLES.index(old):
        return False
    if actor_role == "owner":
        return True
    return actor_role == "admin" and not actor_is_target and old != "owner" and new != "owner"


def may_remove(actor_role: str, actor_is_target: bool, target_role: str) -> bool:
    if actor_is_target:
        return True  # leaving; the database refuses the last owner
    if target_role == "owner":
        return actor_role == "owner"
    return actor_role in MANAGERS
