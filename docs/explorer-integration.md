# Integració amb Bolets Explorador

## URL pública

Després d’activar GitHub Pages (branch `main` / carpeta `/docs`, o Actions Pages):

```text
https://<user>.github.io/bolets-mc-data/panel.json
```

Exemple (substituïu `<user>` pel vostre usuari o org de GitHub):

```text
https://jordi-noya.github.io/bolets-mc-data/panel.json
```

## Fetch en viu

L’explorador ha de deixar d’incrustar el panell MC a l’HTML i carregar-lo en remot:

```js
window.__MC_REMOTE_URL = "https://<user>.github.io/bolets-mc-data/panel.json";
```

Després, en el boot / bridge MC:

```js
async function loadMcRemote() {
  const url = window.__MC_REMOTE_URL;
  if (!url) return null;
  const res = await fetch(url, { cache: "no-cache" });
  if (!res.ok) throw new Error("MC remote " + res.status);
  const pan = await res.json();
  window.__MC_PAYLOAD = pan;
  return pan;
}
```

El JSON té el mateix schema que l’antic `#mc-panel` embegut:

- `stations`, `days`, `series`, `checksum`, `version`, `built`, `ccaa`

## CORS

GitHub Pages serveix amb capçaleres CORS permissives per a GET estàtic; `fetch` des de qualsevol origen de l’explorador hauria de funcionar.

## Actualització

L’Action diària (~23:30 Madrid / cron 21:30 UTC) afegeix un dia a `data/daily/` i regenera `panel.json`. L’explorador veu dies nous al proper reload.
