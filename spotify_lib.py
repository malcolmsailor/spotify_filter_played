import os

import tekore as tk

CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".config", "spotify_filter_played")

if not os.path.exists(CONFIG_DIR):
    os.makedirs(CONFIG_DIR)

AUTH_CONFIG = os.path.join(CONFIG_DIR, "auth.cfg")


def init_auth(reauthenticate):
    user_token = None
    if not reauthenticate and os.path.exists(AUTH_CONFIG):
        (
            client_id,
            client_secret,
            redirect_uri,
            user_refresh,
        ) = tk.config_from_file(AUTH_CONFIG, return_refresh=True)
        user_token = tk.refresh_user_token(client_id, client_secret, user_refresh)
    if user_token is None:
        client_id = input("Paste client id: ")
        client_secret = input("Paste client secret: ")
        redirect_uri = "https://example.com/callback"
        user_token = tk.prompt_for_user_token(
            client_id,
            client_secret,
            redirect_uri,
            scope=tk.scope.every,  # type:ignore
        )
        conf = (
            client_id,
            client_secret,
            redirect_uri,
            user_token.refresh_token,
        )
        tk.config_to_file(AUTH_CONFIG, conf)

    return client_id, client_secret, user_token  # type:ignore
