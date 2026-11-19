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
  // The ONLYOFFICE editing sidecar serves at the root of its own hostname
  // (FileX refuses a frame on its own origin — measured). No gallery shot
  // points at it standalone (the welcome page is off); the editor appears
  // inside the filex-office shots as the frame's origin.
  onlyoffice: 'office',
  docuseal: 'docuseal',
  litellm: 'litellm',
};

const SHOTS = [
  // ---- Dokploy control plane ---------------------------------------------
  {
    shot: 'dokploy-login', area: 'dokploy', service: 'dokploy', viewportOnly: true, noAuth: true,
    caption: 'Dokploy — the control plane the wizard itself installs through. One operator password per box.',
    steps: [{ goto: '/' }],
  },
  {
    shot: 'dokploy-projects', area: 'dokploy', service: 'dokploy',
    caption: 'Every wizard service is a Dokploy project — inspect and restart it by hand whenever you like.',
    steps: [{ waitForSelector: 'text=/Projects/i', timeout: 60000 }, { wait: 2500 }],
  },

  // ---- Coder workspaces ----------------------------------------------------
  {
    shot: 'coder-login', area: 'coder', service: 'coder', viewportOnly: true, noAuth: true,
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
      { goto: '/workspaces' }, { wait: 2000 },
      { click: 'a[href*="/@"]' }, { wait: 2500 },
    ],
  },
  {
    shot: 'coder-template-terraform', area: 'coder', service: 'coder',
    caption: 'The template is plain Terraform — the wizard bakes the box topology into it at plan time.',
    steps: [
      { goto: '/templates/ubuntu-vscode-hermes-desktop' }, { wait: 1500 },
      { click: 'text=Source Code' }, { wait: 2000 },
    ],
  },
  {
    shot: 'coder-users', area: 'coder', service: 'coder',
    caption: 'Users page: the primary deployer account plus a joined second person — each gets their own identity lane.',
    steps: [{ goto: '/users' }, { wait: 1500 }],
  },
  {
    shot: 'coder-schedule', area: 'coder', service: 'coder',
    caption: 'Workspace settings are the day-2 knobs — update policy first; schedule and TTL controls sit alongside.',
    // Box-measured kebab menu: Stop…/Restart…/Settings/… and the settings
    // page anchors on General / Automatic Updates / Update Policy headings.
    steps: [
      { goto: '/workspaces' }, { wait: 2500 },
      { hover: 'table tbody tr' },
      { click: 'button:has-text("Workspace actions")' }, { wait: 700 },
      { click: '[role="menuitem"]:has-text("Settings")' },
      { waitForSelector: 'text=/Automatic Updates/i', timeout: 30000 }, { wait: 1500 },
    ],
  },

  // ---- Gitea identities ------------------------------------------------------
  {
    shot: 'gitea-logged-out', area: 'gitea', service: 'gitea', viewportOnly: true, noAuth: true,
    caption: 'A logged-out visitor meets a 404 on the wizard-seeded repo: private by default, so the anonymous web sees no listing and no page.',
    // Box-measured: on this Gitea build a logged-out /explore renders a
    // closed listing (303 to the repos tab, no listing, navbar Sign In —
    // no sign-in form), and a private repo or profile answers a plain 404
    // to the anonymous request. The 404 on the boot lane's seed is the
    // privacy proof that needs no interpretation, so that is what this
    // shot photographs.
    steps: [
      { goto: '/{owner}/wizard-demo' },
      { waitForSelector: 'text=/not found/i', wait: 1200 },
    ],
  },
  {
    shot: 'gitea-home', area: 'gitea', service: 'gitea',
    caption: 'Team Git, self-hosted — the pack provisions itself and the wizard keeps its admin hat in a state file.',
    steps: [{ goto: '/' }, { wait: 1200 }],
  },
  {
    shot: 'gitea-profile', area: 'gitea', service: 'gitea',
    caption: 'A workspace owner wakes up a Gitea account already theirs: login name, real email primary and verified.',
    // Box-measured: Gitea 28.x serves the profile form at /user/settings
    // itself (/user/settings/profile 404s — caught in vision review).
    steps: [
      { goto: '/user/settings' }, { wait: 1500 },
    ],
  },
  {
    shot: 'gitea-identities', area: 'gitea', service: 'gitea',
    caption: 'Emails are managed identities, not placeholders — the boot lane migrates anything the pre-fix wizard left behind.',
    steps: [
      { goto: '/user/settings/account' }, { wait: 1500 },
    ],
  },
  {
    shot: 'gitea-keys', area: 'gitea', service: 'gitea',
    caption: 'One deploy key pair per (user, workspace), titled by the pair — push over SSH, nothing shared.',
    steps: [{ goto: '/user/settings/keys' }, { wait: 1200 }],
  },
  {
    shot: 'gitea-provisioner', area: 'gitea', service: 'gitea',
    caption: 'Every account on the box, listed: the people and nobody else — the wizard provisions under the deployer\'s own login, so there is no service account to audit.',
    steps: [{ goto: '/explore/users' }, { wait: 1200 }],
  },
  {
    shot: 'gitea-repo-commits', area: 'gitea', service: 'gitea',
    caption: 'Commits authored in the workspace land with the person, over their workspace key.',
    steps: [{ goto: '/{owner}/wizard-demo/commits/branch/main' }, { wait: 1500 }],
  },

  // ---- LiteLLM gateway -------------------------------------------------------
  {
    shot: 'litellm-login', area: 'litellm', service: 'litellm', viewportOnly: true, noAuth: true,
    caption: 'LiteLLM admin UI login.',
    steps: [{ goto: '/ui/login/' }],
  },
  {
    shot: 'litellm-models', area: 'litellm', service: 'litellm',
    caption: 'Model autosync registers provider models as sync/* — including a tailnet vLLM box named for the host that serves it.',
    // Box-measured: 75 synced models paginate alphabetically and the tailnet
    // row sits deep; the search filter brings the host-named sync/* model
    // into frame, and its label then lands inside the redaction rects.
    steps: [
      { goto: '/ui/?page=models' }, { waitForSelector: 'table tbody tr', wait: 2000 },
      { click: 'button:has-text("×")', optional: true },
      { fill: 'input[placeholder*="Search model" i]', value: 'tux' },
      { wait: 3000 },
    ],
  },
  {
    shot: 'litellm-keys', area: 'litellm', service: 'litellm',
    caption: 'Every app gets its own virtual key — coder-hermes, dokploy-ai — plus one minted per (user, workspace), spend tracked per key.',
    // The feedback popup anchors bottom-right over the table; dismissed if
    // present (optional), and the workspace alias blurs its dotless domain.
    steps: [
      { goto: '/ui/?page=api-keys' }, { waitForSelector: 'table tbody tr', wait: 2500 },
      { click: 'text="Don\'t ask me again"', optional: true },
      { click: 'button:has-text("×")', optional: true }, { wait: 500 },
    ],
  },
  {
    shot: 'litellm-key-detail', area: 'litellm', service: 'litellm',
    caption: 'A key carries its model allow-list; the team plan grants it, the box enforces it.',
    // Box-measured: alias cells render inside a CSS-truncated button, so the
    // row itself is the reliable click target for the detail drawer.
    steps: [
      { goto: '/ui/?page=api-keys' }, { waitForSelector: 'table tbody tr', wait: 2500 },
      { click: 'table tbody tr:has-text("coder-hermes") button' }, { wait: 2500 },
      { click: 'text="Don\'t ask me again"', optional: true },
      { click: 'button:has-text("×")', optional: true },
    ],
  },
  {
    shot: 'litellm-usage', area: 'litellm', service: 'litellm',
    caption: 'Spend and tokens per key — who called which model, on whose box.',
    steps: [
      { goto: '/ui/?page=new_usage' }, { waitForSelector: 'canvas, table', wait: 4000 },
      { click: 'text="Don\'t ask me again"', optional: true },
      { click: 'button:has-text("×")', optional: true },
    ],
  },
  {
    shot: 'litellm-logs', area: 'litellm', service: 'litellm',
    caption: 'Request logs: every completion, keyed to the app and person that issued it.',
    steps: [
      { goto: '/ui/?page=logs' }, { waitForSelector: 'table', wait: 3000 },
      { click: 'text="Don\'t ask me again"', optional: true },
      { click: 'button:has-text("×")', optional: true },
    ],
  },
  {
    shot: 'litellm-users', area: 'litellm', service: 'litellm',
    caption: 'New-user policy is a stack knob: models off by default, grants explicit, the admin exempt.',
    steps: [
      { goto: '/ui/?page=users' }, { waitForSelector: 'table tbody tr', wait: 3000 },
      { click: 'text="Don\'t ask me again"', optional: true },
      { click: 'button:has-text("×")', optional: true },
    ],
  },
  {
    shot: 'litellm-internal-user', area: 'litellm', service: 'litellm',
    caption: 'One identity per person across the box: every joined human is an internal_user here and everywhere; the deployer is the only proxy_admin.',
    // The users table's Role column is the whole story — every non-admin
    // row must read internal_user, the one-ID contract's LiteLLM side.
    steps: [
      { goto: '/ui/?page=users' }, { waitForSelector: 'table tbody tr', wait: 3000 },
      { click: 'text="Don\'t ask me again"', optional: true },
      { click: 'button:has-text("×")', optional: true },
    ],
  },

  // ---- FileX shared drive -----------------------------------------------------
  {
    shot: 'filex-login', area: 'filex', service: 'filex', viewportOnly: true, noAuth: true,
    caption: 'FileX login — the deployer signs in as the account the pack bootstrapped; nothing else to remember.',
    // Box-measured: /admin/login hydrates its form ~1s after first paint —
    // capture before that and you photograph a blank page.
    steps: [
      { goto: '/admin/login' },
      { waitForSelector: 'input[name="email"]' },
      { wait: 1200 },
    ],
  },
  {
    shot: 'filex-files', area: 'filex', service: 'filex',
    caption: '~/shared is FileX: versioned objects on SeaweedFS S3, mounted over rclone FUSE.',
    steps: [
      { goto: '/admin/home' }, { wait: 2000 },
      { click: 'text="My files"' }, { wait: 2000 },
    ],
  },
  {
    shot: 'filex-versions', area: 'filex', service: 'filex',
    caption: 'Two people edit the same file; FileX keeps every version and every author — this is where the act pays off.',
    // Box-measured (twice): the /admin/files history-search input hydrates
    // late (search backend), while /admin/home -> My files renders instantly.
    // The browser route also shows the shared folder the search page hid.
    steps: [
      { goto: '/admin/home' }, { wait: 2500 },
      { click: 'text="My files"' },
      { waitForSelector: 'text=/shared-act/', timeout: 25000 }, { wait: 1000 },
      { click: 'text=/shared-act/' }, { wait: 2000 },
      { click: 'button[aria-label*="detail" i], button[aria-label*="info" i], button[title*="detail" i]', optional: true },
      { click: 'text="Activity"', optional: true },
      { wait: 2500 },
    ],
  },
  {
    shot: 'filex-connect', area: 'filex', service: 'filex',
    caption: 'The same bucket speaks S3 and WebDAV — official desktop and mobile clients connect with the user\'s own key.',
    steps: [
      { goto: '/admin/home' }, { wait: 2000 },
      { click: 'text="Connections"' }, { wait: 2500 },
    ],
  },
  {
    shot: 'filex-s3keys', area: 'filex', service: 'filex',
    caption: 'S3 keys are per (user, workspace) — the label names both, and the mount dies with the workspace, not the person.',
    steps: [
      { goto: '/admin/home' }, { wait: 2000 },
      { click: 'text="API keys"' }, { wait: 2500 },
    ],
  },
  {
    shot: 'filex-people', area: 'filex', service: 'filex',
    caption: 'People page after a clean install: only humans — the deployer\'s own email and the joined second person. The provisioner account is gone; its lane rides an admin-scope token on the admin itself.',
    steps: [{ goto: '/admin/users' }, { wait: 2000 }],
  },
  {
    shot: 'filex-office-edit', area: 'filex', service: 'filex',
    caption: 'Open a document: the bundled ONLYOFFICE editor arrives in a frame on its own hostname — one sidecar per box, JWT on every hop.',
    // The frame's origin is the whole proof of the sidecar design: FileX
    // refuses an editor on its own origin (measured crash-loop), so the
    // iframe src points at office.<root-domain>; the redaction scan
    // catches the root domain inside the attribute. A .docx must exist in
    // My files before the shoot (upload during shot prep).
    steps: [
      { goto: '/admin/home' }, { wait: 2500 },
      { click: 'text="My files"' }, { wait: 2000 },
      { click: 'text=/\\.docx/' }, { wait: 2500 },
      { click: 'text=/^edit$|open in editor|edit in onlyoffice/i', optional: true },
      { waitForSelector: 'iframe[src*="web-apps"], iframe[src*="office"]', timeout: 30000 },
      { wait: 4000 },
    ],
  },
  {
    shot: 'filex-office-versions', area: 'filex', service: 'filex',
    caption: 'Every save in the editor becomes a FileX version — the office session signs back into the same bucket as the same person.',
    steps: [
      { goto: '/admin/home' }, { wait: 2500 },
      { click: 'text="My files"' },
      { waitForSelector: 'text=/\\.docx/', timeout: 25000 }, { wait: 1000 },
      { click: 'text=/\\.docx/' }, { wait: 2000 },
      { click: 'button[aria-label*="detail" i], button[aria-label*="info" i], button[title*="detail" i]', optional: true },
      { click: 'text="Activity"', optional: true },
      { wait: 2500 },
    ],
  },

  // ---- DocuSeal ---------------------------------------------------------------
  {
    shot: 'docuseal-login', area: 'docuseal', service: 'docuseal', viewportOnly: true, noAuth: true,
    caption: 'DocuSeal — forms and signatures on your own metal.',
    // Box-measured: this build serves login at /sign_in; /account/sign_in
    // renders the app's own 404 page.
    steps: [{ goto: '/sign_in' }],
  },
  {
    shot: 'docuseal-forms', area: 'docuseal', service: 'docuseal',
    caption: 'Form builder, submissions, and audit trail; the wizard bootstraps the deployer account.',
    // Box-measured: '/' doubles as marketing page AND dashboard — the
    // "Document Templates" heading only renders for a live session, so
    // waiting on it is the fail-closed proof of authentication.
    steps: [{ goto: '/' }, { waitForSelector: 'text=/Document Templates/i', wait: 2000 }],
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
    shot: 'walkthrough-login', area: 'walkthrough', service: 'coder', viewportOnly: true, noAuth: true,
    caption: 'Demo account signs in — the primary deployer account created it minutes before.',
    steps: [{ goto: '/login' }],
    loginAs: () => ({ login: 'demo', email: 'demo@wizard.local.test', password: 'demo-demo-demo' }),
  },
  {
    shot: 'walkthrough-workspace', area: 'walkthrough', service: 'coder',
    caption: 'One workspace per person, from the one seeded template.',
    steps: [{ goto: '/workspaces' }, { wait: 1500 }],
    loginAs: () => ({ login: 'demo', email: 'demo@wizard.local.test', password: 'demo-demo-demo' }),
  },
  {
    shot: 'walkthrough-gitea-profile', area: 'walkthrough', service: 'gitea',
    caption: 'The identity lane springs again: demo gets a Gitea account with demo\'s own real email primary.',
    steps: [{ goto: '/user/settings' }, { wait: 1500 }],
    // Box-measured + template lines 678-716/849-860: Gitea accounts ride the
    // STACK ADMIN password (one operator credential across the stack), while
    // the Coder shots above sign in with demo's own workspace password.
    loginAs: (c) => ({ login: 'demo', email: 'demo@wizard.local.test', password: c.password }),
  },
];

module.exports = { SHOTS, SERVICE_SUBDOMAINS };
