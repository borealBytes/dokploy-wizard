// dokploy-wizard screenshot studio — the playbook.
//
// One declarative entry per gallery image: area (output folder), url path,
// scripted steps, captions exactly as the README should read them, and the
// redaction scan's expectations. Captions spell the root domain as
// «your-domain» so no committed text carries an operator's identity.
//
// Steps: goto, wait (ms), waitForSelector, click, fill (+value), press,
// shotAfter (extra settle ms). Extra blur rectangles: `blur: [selector]`.
// `terminal` shots render a CLI transcript deterministically (no browser
// history, no tmux races) through the same redaction pipeline.

const SERVICE_SUBDOMAINS = {
  dokploy: 'dokploy',
  coder: 'coder',
  gitea: 'gitea',
  filex: 'filex',
  docuseal: 'docuseal',
  litellm: 'litellm',
};

const SHOTS = [
  // ---- Dokploy control plane ---------------------------------------------
  {
    shot: 'dokploy-login', area: 'dokploy', service: 'dokploy', viewportOnly: true,
    caption: 'Dokploy — the control plane the wizard itself installs through. One operator password per box.',
    steps: [{ goto: '/' }],
  },
  {
    shot: 'dokploy-projects', area: 'dokploy', service: 'dokploy',
    caption: 'Every wizard service is a Dokploy project — inspect and restart it by hand whenever you like.',
    steps: [{ goto: '/' }, { wait: 1500 }],
  },

  // ---- Coder workspaces ----------------------------------------------------
  {
    shot: 'coder-login', area: 'coder', service: 'coder', viewportOnly: true,
    caption: 'Coder login — the same email and password as every other surface.',
    steps: [{ goto: '/login' }],
  },
  {
    shot: 'coder-dashboard', area: 'coder', service: 'coder',
    caption: 'The wizard seeds one template and one default workspace; the dev box boots in seconds.',
    steps: [{ goto: '/workspaces' }, { wait: 2000 }],
  },
  {
    shot: 'coder-tiles', area: 'coder', service: 'coder',
    caption: 'App tiles ride the workspace: OpenCode, Hermes, Gitea, FileX, DocuSeal — per-person, pre-authenticated.',
    steps: [
      { goto: '/workspaces' }, { wait: 1500 },
      { click: 'a[href*="default"]' }, { waitForSelector: 'table button, [data-testid="workspace-actions"]', wait: 0 },
      { click: 'button[aria-label="Open menu items"]' }, { wait: 300 },
      { click: 'text=/^Startup config$/' }, { wait: 1500 },
    ],
  },
  {
    shot: 'coder-template-terraform', area: 'coder', service: 'coder',
    caption: 'The template is plain Terraform — the wizard bakes the box topology into it at plan time.',
    steps: [
      { goto: '/templates' }, { wait: 1000 },
      { click: 'text=ubuntu-vscode-hermes-desktop' }, { wait: 1000 },
    ],
  },
  {
    shot: 'coder-users', area: 'coder', service: 'coder',
    caption: 'Users page: the primary deployer account plus a joined second person — each gets their own identity lane.',
    steps: [{ goto: '/users' }, { wait: 1200 }],
  },
  {
    shot: 'coder-schedule', area: 'coder', service: 'coder',
    caption: 'Day-2 workspace hygiene: auto-start weekdays, auto-stop nights — nothing idles unpaid.',
    steps: [
      { goto: '/workspaces' }, { wait: 1500 },
      { click: 'a[href*="default"]' }, { wait: 800 },
      { click: 'button[aria-label="Open menu items"]' }, { wait: 300 },
      { click: 'text=/Schedule$/' }, { wait: 1200 },
    ],
  },

  // ---- Gitea identities ------------------------------------------------------
  {
    shot: 'gitea-home', area: 'gitea', service: 'gitea',
    caption: 'Team Git, self-hosted — the pack provisions itself and the wizard keeps its admin hat in a state file.',
    steps: [{ goto: '/' }, { wait: 1200 }],
  },
  {
    shot: 'gitea-profile', area: 'gitea', service: 'gitea',
    caption: 'A workspace owner wakes up a Gitea account already theirs: login name, real email primary and verified.',
    steps: [
      { goto: '/user/settings/profile' }, { wait: 1200 },
    ],
  },
  {
    shot: 'gitea-identities', area: 'gitea', service: 'gitea',
    caption: 'Emails are managed identities, not placeholders — the boot lane migrates anything the pre-fix wizard left behind.',
    steps: [
      { goto: '/user/settings/emails' }, { wait: 1200 },
    ],
  },
  {
    shot: 'gitea-keys', area: 'gitea', service: 'gitea',
    caption: 'One deploy key pair per (user, workspace), titled by the pair — push over SSH, nothing shared.',
    steps: [{ goto: '/user/settings/keys' }, { wait: 1200 }],
  },
  {
    shot: 'gitea-provisioner', area: 'gitea', service: 'gitea',
    caption: 'The dokploy-wizard provisioner is an ordinary admin account — audit it, rotate it, delete it.',
    steps: [{ goto: '/dashboards/admin/users' }, { wait: 1500 }],
  },
  {
    shot: 'gitea-repo-commits', area: 'gitea', service: 'gitea',
    caption: 'Commits authored in the workspace land with the person, over their workspace key.',
    steps: [{ goto: '/{owner}/wizard-demo/commits' }, { wait: 1200 }],
  },

  // ---- LiteLLM gateway -------------------------------------------------------
  {
    shot: 'litellm-login', area: 'litellm', service: 'litellm', viewportOnly: true,
    caption: 'LiteLLM admin UI login.',
    steps: [{ goto: '/ui/login' }],
  },
  {
    shot: 'litellm-models', area: 'litellm', service: 'litellm',
    caption: 'Model autosync registers provider models as sync/* — including a tailnet vLLM box named for the host that serves it.',
    steps: [{ goto: '/ui/models' }, { waitForSelector: 'table', wait: 2000 }],
  },
  {
    shot: 'litellm-keys', area: 'litellm', service: 'litellm',
    caption: 'Every app gets its own virtual key — coder-hermes, coder-opencode — minted per (user, workspace), spend tracked per key.',
    steps: [{ goto: '/ui/virtualKeys' }, { waitForSelector: 'table', wait: 2000 }],
  },
  {
    shot: 'litellm-key-detail', area: 'litellm', service: 'litellm',
    caption: 'A key carries its model allow-list; the team plan grants it, the box enforces it.',
    steps: [
      { goto: '/ui/virtualKeys' }, { waitForSelector: 'table', wait: 1500 },
      { click: 'text=coder-hermes' }, { wait: 1500 },
    ],
  },
  {
    shot: 'litellm-usage', area: 'litellm', service: 'litellm',
    caption: 'Spend and tokens per key — who called which model, on whose box.',
    steps: [{ goto: '/ui/usage' }, { waitForSelector: 'canvas, table', wait: 2500 }],
  },
  {
    shot: 'litellm-logs', area: 'litellm', service: 'litellm',
    caption: 'Request logs: every completion, keyed to the app and person that issued it.',
    steps: [{ goto: '/ui/logs' }, { waitForSelector: 'table', wait: 2000 }],
  },
  {
    shot: 'litellm-users', area: 'litellm', service: 'litellm',
    caption: 'New-user policy is a stack knob: models off by default, grants explicit, the admin exempt.',
    steps: [{ goto: '/ui/users' }, { waitForSelector: 'table', wait: 2000 }],
  },
  {
    shot: 'hermes-model-picker', area: 'litellm', service: 'coder',
    caption: 'Workspace apps pick the synced catalog by its honest name — sync/<host>-<model> is a machine you can ping.',
    steps: [
      { goto: '/workspaces' }, { wait: 1500 },
      { click: 'a[href*="default"]' }, { wait: 800 },
      { click: 'button[aria-label="Open menu items"]' }, { wait: 300 },
      { click: 'text=/^App status$/' }, { wait: 1000 },
    ],
  },

  // ---- FileX shared drive -----------------------------------------------------
  {
    shot: 'filex-login', area: 'filex', service: 'filex', viewportOnly: true,
    caption: 'FileX login — the deployer signs in as the account the pack bootstrapped; nothing else to remember.',
    steps: [{ goto: '/login' }],
  },
  {
    shot: 'filex-files', area: 'filex', service: 'filex',
    caption: '~/shared is FileX: versioned objects on SeaweedFS S3, mounted over rclone FUSE.',
    steps: [{ goto: '/files' }, { wait: 2000 }],
  },
  {
    shot: 'filex-versions', area: 'filex', service: 'filex',
    caption: 'Two people edit the same file; FileX keeps every version and every author — this is where the act pays off.',
    steps: [
      { goto: '/files' }, { wait: 1500 },
      { click: 'text=shared-act' }, { wait: 1200 },
    ],
  },
  {
    shot: 'filex-connect', area: 'filex', service: 'filex',
    caption: 'The same bucket speaks S3 and WebDAV — official desktop and mobile clients connect with the user\'s own key.',
    steps: [{ goto: '/clients' }, { wait: 1500 }],
  },
  {
    shot: 'filex-s3keys', area: 'filex', service: 'filex',
    caption: 'S3 keys are per (user, workspace) — the label names both, and the mount dies with the workspace, not the person.',
    steps: [{ goto: '/account/settings' }, { wait: 1500 }],
  },

  // ---- DocuSeal ---------------------------------------------------------------
  {
    shot: 'docuseal-login', area: 'docuseal', service: 'docuseal', viewportOnly: true,
    caption: 'DocuSeal — forms and signatures on your own metal.',
    steps: [{ goto: '/account/sign_in' }],
  },
  {
    shot: 'docuseal-forms', area: 'docuseal', service: 'docuseal',
    caption: 'Form builder, submissions, and audit trail; the wizard bootstraps the deployer account.',
    steps: [{ goto: '/' }, { wait: 1500 }],
  },

  // ---- The ~/shared cross-user act ---------------------------------------------
  {
    shot: 'shared-write', area: 'shared', terminal: true,
    caption: 'Workspace A writes into ~/shared — an ordinary file write over the FUSE mount.',
    steps: [],
    terminal: {
      title: 'coder (clayton@default) — ~/shared',
      lines: [
        '$ echo "kickoff checklist v1" > ~/shared/shared-act.txt',
        '$ ls -l ~/shared/shared-act.txt',
        '-rw-r--r-- 1 coder coder 21 …  shared-act.txt',
      ],
    },
  },
  {
    shot: 'shared-seen-b', area: 'shared', terminal: true,
    caption: 'Workspace B sees the same bytes seconds later — one bucket, one identity map, no sync daemon.',
    steps: [],
    terminal: {
      title: 'coder (demo@default) — ~/shared',
      lines: [
        '$ cat ~/shared/shared-act.txt',
        'kickoff checklist v1',
        '$ shared-versions shared-act.txt',
        'v3 21B  clayton@«root»  minutes ago',
        'v2 19B  demo@«root»     earlier',
        'v1 0B   clayton@«root»  first write',
      ],
    },
  },

  // ---- Tailnet & local model source ---------------------------------------------
  {
    shot: 'tailscale-status', area: 'tailnet', terminal: true,
    caption: 'The box joins your tailnet on install — private host access, and private model servers become reachable.',
    steps: [],
    terminal: {
      title: 'root@«root-host»',
      lines: [
        '# tailscale status',
        '100.x.y.z   «box-label»      operator@  linux   -',
        '100.x.y.w   «laptop-label»   operator@  linux   offline',
        '100.x.y.v   tuxdesktop       operator@  linux   idle, tx:… rx:…',
      ],
    },
  },
  {
    shot: 'local-model-sync', area: 'tailnet', terminal: true,
    caption: 'A tailnet vLLM/Ollama endpoint is a first-class sync source: models register under the serving host\'s name.',
    steps: [],
    terminal: {
      title: '$ curl -s …/v1/model/info | jq',
      lines: [
        '{',
        '  "model_name": "sync/tuxdesktop-Qwen3.8-Flash-Next-Uncensored",',
        '  "litellm_params": {',
        '    "api_base": "http://tuxdesktop.felis-aeolian.ts.net:30000/v1",',
        '    "litellm_metadata": {',
        '      "managed_by": "dokploy-wizard-model-sync",',
        '      "supports_system_message": false',
        '    }',
        '  }',
        '}',
      ],
    },
  },

  // ---- Day-2 lifecycle panel ---------------------------------------------------------
  {
    shot: 'cli-modify-noop', area: 'day2', terminal: true,
    caption: 'modify re-runs the whole convergence loop — an unchanged box answers in seconds, mode: noop.',
    steps: [],
    terminal: {
      title: './bin/dokploy-wizard modify --env-file ./.install.env --non-interactive',
      lines: [
        'wizard mode: noop',
        'duration_s: 21.4',
        'healthy_services: 12',
        'no drift: desired == applied',
      ],
    },
  },
  {
    shot: 'cli-upgrade-check', area: 'day2', terminal: true,
    caption: 'upgrade --check is read-only: it tells you the pinned LiteLLM drifted and what the bump would touch.',
    steps: [],
    terminal: {
      title: './bin/dokploy-wizard upgrade --env-file ./.install.env --check',
      lines: [
        'litellm pin: v1.83.0-stable -> candidate v1.84.0-stable',
        'drift: gateway restart required; consumer keys survive',
        'no changes applied (--check is read-only)',
      ],
    },
  },
  {
    shot: 'cli-uninstall', area: 'day2', terminal: true,
    caption: 'Uninstall is ledger-scoped: it removes what it owns, and a confirm file stands between you and a typo.',
    steps: [],
    terminal: {
      title: './bin/dokploy-wizard uninstall --retain-data --non-interactive --confirm-file fixtures/retain.confirm',
      lines: [
        'ownership ledger: 24 resources, all wizard-created',
        'retained volumes: seaweedfs-data, gitea-data, coder-home',
        'state kept at /root/dokploy-wizard/state for audit',
      ],
    },
  },

  // ---- New-account identity lane (walkthrough strip) -------------------------------
  {
    shot: 'walkthrough-login', area: 'walkthrough', service: 'coder', viewportOnly: true,
    caption: 'Demo account signs in — the primary deployer account created it minutes before.',
    steps: [{ goto: '/login' }],
    loginAs: () => ({ email: 'demo@wizard.local.test', password: 'demo-demo-demo' }),
  },
  {
    shot: 'walkthrough-workspace', area: 'walkthrough', service: 'coder',
    caption: 'One workspace per person, from the one seeded template.',
    steps: [{ goto: '/workspaces' }, { wait: 1500 }],
    loginAs: () => ({ email: 'demo@wizard.local.test', password: 'demo-demo-demo' }),
  },
  {
    shot: 'walkthrough-gitea-profile', area: 'walkthrough', service: 'gitea',
    caption: 'The identity lane springs again: demo gets a Gitea account with demo\'s own real email primary.',
    steps: [{ goto: '/user/settings/profile' }, { wait: 1500 }],
    loginAs: () => ({ email: 'demo@wizard.local.test', password: 'demo-demo-demo' }),
  },
];

module.exports = { SHOTS, SERVICE_SUBDOMAINS };
