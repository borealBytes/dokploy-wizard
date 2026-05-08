from __future__ import annotations

from dokploy_wizard.state import RawEnvInput


def root_install_env() -> RawEnvInput:
    return RawEnvInput(
        format_version=1,
        values={
            "STACK_NAME": "openmerge",
            "ROOT_DOMAIN": "openmerge.me",
            "PACKS": "my-farm-advisor,nextcloud,openclaw,coder,seaweedfs",
            "CODER_WILDCARD_SUBDOMAIN": "*",
            "ENABLE_HEADSCALE": "false",
            "ENABLE_MY_FARM_ADVISOR": "true",
            "ENABLE_TAILSCALE": "true",
            "TAILSCALE_HOSTNAME": "openmerge",
            "TAILSCALE_AUTH_KEY": "tskey-auth-123",
            "HOST_OS_ID": "ubuntu",
            "HOST_OS_VERSION_ID": "24.04",
            "HOST_CPU_COUNT": "6",
            "HOST_MEMORY_GB": "12",
            "HOST_DISK_GB": "150",
            "HOST_DOCKER_INSTALLED": "true",
            "HOST_DOCKER_DAEMON_REACHABLE": "true",
            "HOST_PORT_80_IN_USE": "false",
            "HOST_PORT_443_IN_USE": "false",
            "HOST_PORT_3000_IN_USE": "false",
            "HOST_ENVIRONMENT": "local",
            "CLOUDFLARE_ACCOUNT_ID": "account-123",
            "CLOUDFLARE_API_TOKEN": "token-123",
            "CLOUDFLARE_ZONE_ID": "zone-123",
            "CLOUDFLARE_TUNNEL_NAME": "openmerge-tunnel",
            "CLOUDFLARE_MOCK_ACCOUNT_OK": "true",
            "CLOUDFLARE_MOCK_ZONE_OK": "true",
            "DOKPLOY_API_URL": "https://dokploy.example.com",
            "DOKPLOY_API_KEY": "dokp-test-key",
            "DOKPLOY_BOOTSTRAP_HEALTHY": "true",
            "DOKPLOY_BOOTSTRAP_MOCK_API_KEY": "dokp-test-key",
            "DOKPLOY_MOCK_API_MODE": "true",
            "DOKPLOY_ADMIN_EMAIL": "operator@example.com",
            "DOKPLOY_ADMIN_PASSWORD": "super-secret-password",
            "SEAWEEDFS_ACCESS_KEY": "seaweed-access",
            "SEAWEEDFS_SECRET_KEY": "seaweed-secret",
            "OPENCLAW_CHANNELS": "telegram",
            "ADVISOR_GATEWAY_PASSWORD": "advisor-password",
            "AI_DEFAULT_API_KEY": "shared-ai-key",
            "AI_DEFAULT_BASE_URL": "https://models.example.com/v1",
            "LITELLM_LOCAL_BASE_URL": "http://tuxdesktop.tailb12aa5.ts.net:61434/v1",
            "LITELLM_LOCAL_MODEL": "unsloth-active",
            "LITELLM_ADMIN_SUBDOMAIN": "litellm",
            "OPENCODE_GO_BASE_URL": "https://opencode.ai/zen/go/v1",
            "OPENCODE_GO_API_KEY": "opencode-go-upstream-key",
            "LITELLM_OPENROUTER_MODELS": (
                "openrouter/hunter-alpha=openrouter/openai/gpt-4.1-mini,"
                "openrouter/healer-alpha=openrouter/anthropic/claude-3.5-sonnet"
            ),
            "MY_FARM_ADVISOR_SUBDOMAIN": "farm",
            "MY_FARM_ADVISOR_CHANNELS": "telegram",
            "MY_FARM_ADVISOR_PRIMARY_MODEL": "openrouter/hunter-alpha",
            "NVIDIA_BASE_URL": "https://integrate.api.nvidia.com/v1",
        },
    )
