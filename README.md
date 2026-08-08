# Frame Atelier — Home Assistant add-on repository

This folder is a **Home Assistant add-on repository**. Add its URL under
**Settings → Add-ons → Add-on Store → ⋮ → Repositories**, then install the
**Frame Atelier** add-on from the store.

```
HA/
├── repository.yaml        Marks this folder as an add-on repo
└── frame_atelier/         The add-on itself
    ├── config.yaml        Add-on manifest (Ingress, ports, arch)
    ├── build.yaml         Base images per architecture
    ├── Dockerfile
    ├── requirements.txt
    ├── run.sh             Container entrypoint
    ├── app.py             Flask backend + Samsung TV control
    ├── www/index.html     The web UI (served through Ingress)
    └── README.md          Add-on documentation
```

> **Note on HACS:** HACS installs *integrations, dashboard cards and themes* —
> it does **not** install add-ons. Add-ons (which is what this is, because it
> needs a running backend to talk to the TV) are installed through the **Add-on
> Store** using the custom-repository steps above, not through HACS.

To publish it: push this `HA/` folder to a public GitHub repo and use that
repo's URL as the custom repository. Update the `url`/`maintainer` fields in
`repository.yaml` and `frame_atelier/config.yaml` to your own.
