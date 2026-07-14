"""
Simple shared username/password gate for the dashboard.

Credentials live in Streamlit secrets (never committed):
  • locally  -> app/.streamlit/secrets.toml
  • on Streamlit Community Cloud -> the app's "Secrets" settings box

Expected shape:
  [auth]
  username = "someuser"
  password = "somepassword"

On Hugging Face Spaces (which exposes secrets as environment variables,
not via st.secrets) set instead:
  AUTH_USERNAME = someuser
  AUTH_PASSWORD = somepassword

Usage (from dashboard.py, right after st.set_page_config):
  from auth import require_login
  require_login()
"""

import hmac
import os

import streamlit as st


def _get_expected():
    """Return (username, password) from secrets/env, or None if not configured."""
    # 1) Streamlit secrets.toml (local + Streamlit Community Cloud)
    try:
        auth = st.secrets["auth"]
        return str(auth["username"]), str(auth["password"])
    except Exception:  # noqa: S110 — deliberate fallback: any secrets error => try env next
        pass
    # 2) Environment variables (Hugging Face Spaces secrets, containers, etc.)
    user, pw = os.environ.get("AUTH_USERNAME"), os.environ.get("AUTH_PASSWORD")
    if user and pw:
        return user, pw
    return None


def _credentials_valid(user: str, pw: str) -> bool:
    expected = _get_expected()
    if expected is None:
        st.session_state["_auth_misconfigured"] = True
        return False
    exp_user, exp_pw = expected
    # constant-time comparison to avoid leaking length/prefix via timing
    ok_user = hmac.compare_digest(user, exp_user)
    ok_pw = hmac.compare_digest(pw, exp_pw)
    return ok_user and ok_pw


def _login_form():
    st.markdown(
        "<div style='max-width:380px;margin:12vh auto 0;'>",
        unsafe_allow_html=True,
    )
    st.markdown("### ⚡ CAISO Market Surveillance")
    st.caption("This dashboard is private. Please sign in to continue.")
    with st.form("login", clear_on_submit=False):
        user = st.text_input("Username", key="_auth_user")
        pw = st.text_input("Password", type="password", key="_auth_pass")
        submitted = st.form_submit_button("Sign in", use_container_width=True)
    if submitted:
        if _credentials_valid(user, pw):
            st.session_state["_auth_ok"] = True
            # don't retain the raw password in session state
            st.session_state.pop("_auth_pass", None)
            st.rerun()
        elif st.session_state.get("_auth_misconfigured"):
            st.error(
                "Login is not configured on this deployment "
                "(missing `[auth]` secrets). Contact the app owner."
            )
        else:
            st.error("Incorrect username or password.")
    if st.session_state.get("_auth_misconfigured") and not submitted:
        st.warning(
            "No credentials are configured yet. Add an `[auth]` section to "
            "`.streamlit/secrets.toml` (see `secrets.toml.example`)."
        )
    st.markdown("</div>", unsafe_allow_html=True)


def require_login():
    """Block the app until a valid shared username/password is entered."""
    if st.session_state.get("_auth_ok"):
        _logout_control()
        return
    _login_form()
    st.stop()


def _logout_control():
    with st.sidebar:
        st.markdown("---")
        if st.button("Sign out", use_container_width=True):
            st.session_state["_auth_ok"] = False
            st.rerun()
