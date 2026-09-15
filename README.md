# pieces

Bot Telegram qui range mes documents personnels et me les renvoie à la demande.

J'envoie un PDF au bot — CV, permis, avis d'impôt, billet d'avion — il le
classe et le stocke. Plus tard je lui demande « envoie-moi mon permis » et il
me le renvoie dans le chat.

## Comment ça marchera

```
PDF envoyé au bot
      ↓
extraction du texte (pdftotext en local, OCR distant si c'est un scan)
      ↓
classement + champs (type, date, émetteur, expiration) par LLM local
      ↓
fichier dans un volume, métadonnées en SQLite
      ↓
recherche plein texte → le PDF revient dans le chat
```

## Décisions prises

- **Python** — l'outillage PDF et OCR y est nettement meilleur qu'en Node.
- **Ollama local pour le classement.** Le tri et la lecture des champs se font
  sur la machine. Demande un GPU utilisable depuis Docker (voir plus bas).
- **L'OCR, lui, est distant** — l'API de Mistral. Ce README disait le contraire
  avant `ext-2` ; le choix a été fait en connaissance de cause. Ce qui a été
  mesuré, sur le même document rasterisé et incliné :

  | inclinaison | Mistral | tesseract en local |
  | :-- | :-- | :-- |
  | droit | 8/8 repères, 0,8 s | 4/4, 4,2 s |
  | 6° | 6/6, 0,9 s | 0/4, texte en bouillie |
  | 12° | 6/6, 0,9 s | rien du tout |

  À quoi s'ajoutent 380 Mo d'image en moins. Le prix est que les documents
  scannés sortent du serveur.
- **SQLite + FTS5** pour la recherche. À ce volume, rien de plus lourd ne se
  justifie.
- Les PDF vivent dans un volume Docker, **jamais dans Git**.

## À savoir avant de coder

- **Les documents sortent du serveur à deux endroits.** Telegram d'abord : les
  bots n'ont pas de chiffrement de bout en bout, les fichiers transitent en
  clair et Telegram en garde une copie. L'OCR ensuite, pour les seuls documents
  scannés, envoyés à l'API Mistral. Les deux sont des choix assumés pour le
  confort et la qualité — mais à ne pas oublier.
- **Whitelist obligatoire**, vérifiée avant tout traitement. Sinon n'importe qui
  connaissant le nom du bot peut demander mon permis. Elle porte sur l'identifiant
  de l'expéditeur, pas sur celui du chat : les deux ne coïncident qu'en privé.
- L'API Bot plafonne le téléchargement à **20 Mo par fichier**.

## État

Le bot n'écoute que moi, range les PDF qu'il reçoit en dédoublonnant, en
extrait le texte, et fait lire les scans par l'OCR. Il ne sait encore ni les
classer, ni les retrouver — `INTENT.md` donne la suite.

## Faire tourner

Il faut Docker, un bot Telegram (créé par `@BotFather`) et une clé d'API
Mistral pour l'OCR.

```sh
git clone https://github.com/Hamid-dev13/pieces.git
cd pieces
cp .env.example .env        # puis renseigner les trois valeurs
./deploy.sh
```

`PIECES_ALLOWED_IDS` attend des identifiants Telegram numériques, pas des
`@pseudo`. Pour obtenir le sien : écrire à `@userinfobot`, ou démarrer le bot
avec une valeur quelconque et lire l'identifiant refusé dans les logs.

Le classement par LLM (à venir) demandera un Ollama joignable et, pour être
utilisable, un GPU accessible depuis Docker :

```sh
sudo apt-get install -y nvidia-container-toolkit
sudo nvidia-ctk runtime configure --runtime=docker
sudo systemctl restart docker
```

Les conteneurs se branchent sur un réseau Docker nommé `homelab`, créé au
besoin — c'est par là qu'Ollama sera joint, par son nom de conteneur.
