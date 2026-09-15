# pieces

Bot Telegram qui range des documents personnels. `README.md` dit quoi et
pourquoi, `INTENT.md` jusqu'où, `CONCEPTION.md` comment.

## Comment on travaille

Une story d'`INTENT.md` à la fois, dans l'ordre. Chacune a un critère
`Done when` qui se vérifie **en essayant**, pas en relisant le code : si le
critère demande un second compte Telegram ou cinq documents réels, la story
n'est pas finie tant que ça n'a pas été fait pour de vrai.

Quand une décision de `CONCEPTION.md` se révèle fausse à l'usage, on corrige le
document plutôt que de laisser le code et la prose diverger. Deux décisions ont
déjà été reprises ainsi : la fusion de `sec-1` et `ing-1`, et le passage à un
OCR distant.

## Déploiement

Le code s'écrit sur un poste, se pousse sur GitHub, et le serveur tire :

```sh
./deploy.sh
```

`git pull --ff-only` puis `docker compose up -d --build`. Rien de plus.

## Secrets

**Aucun secret dans ce dépôt.** Ils vivent dans un `.env` sur le serveur, hors
Git. Quand une variable apparaît, l'ajouter à `.env.example` sans valeur et le
signaler — c'est un humain qui renseigne la vraie valeur.

## Conventions

Français pour la prose et les commentaires, anglais pour les identifiants et le
code. Les colonnes SQL sont en français, comme le schéma de `CONCEPTION.md`.
