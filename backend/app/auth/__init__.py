"""Multi-user auth — JWT bearer + bcrypt password hashing + RBAC.

Public surface — call sites should import from here, not the submodules.
The legacy single-admin HMAC auth (in `core_system.py`) is preserved for
backward compatibility; new code uses these primitives.

Today's roles: owner | admin | viewer.
  - owner   = workspace creator; can manage users, billing, settings
  - admin   = can upload, manage data
  - viewer  = read-only (dashboard + AI Assistant)
"""
from app.auth.passwords import hash_password, verify_password
from app.auth.tokens import (
    Principal,
    decode_token,
    encode_token,
    extract_bearer_token,
)
from app.auth.users import (
    UserRow,
    create_user,
    find_user_by_email,
    get_user_by_id,
    update_last_login,
)
from app.auth.workspaces import (
    WorkspaceRow,
    create_workspace,
    get_workspace_by_id,
)
from app.auth.middleware import (
    require_principal,
    require_role,
    try_principal,
)

__all__ = [
    "Principal",
    "UserRow",
    "WorkspaceRow",
    "create_user",
    "create_workspace",
    "decode_token",
    "encode_token",
    "extract_bearer_token",
    "find_user_by_email",
    "get_user_by_id",
    "get_workspace_by_id",
    "hash_password",
    "require_principal",
    "require_role",
    "try_principal",
    "update_last_login",
    "verify_password",
]
