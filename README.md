# pieces

Bot Telegram qui range mes documents personnels et me les renvoie à la demande.

J'envoie un PDF au bot — CV, permis, avis d'impôt, billet d'avion — il le
classe et le stocke. Plus tard je lui demande « envoie-moi mon permis » et il
me le renvoie dans le chat.

## Comment ça marchera

```
PDF envoyé au bot
      ↓
extraction du texte (pdftotext, OCR en secours si c'est un scan)
      ↓
classement + champs (type, date, émetteur, expiration) par LLM local
      ↓
fichier dans un volume, métadonnées en SQLite
      ↓
recherche plein texte → le PDF revient dans le chat
```

## Décisions prises

- **Python** — l'outillage PDF et OCR y est nettement meilleur qu'en Node.
- **Ollama local**, pas d'API externe : des avis d'impôt et un permis n'ont pas
  à sortir du serveur. Demande d'activer le GPU (voir le README racine).
- **SQLite + FTS5** pour la recherche. À ce volume, rien de plus lourd ne se
  justifie.
- Les PDF vivent dans un volume Docker, **jamais dans Git**.

## À savoir avant de coder

- **Telegram n'est pas chiffré de bout en bout pour les bots.** Les documents
  transitent en clair par les serveurs de Telegram, qui en gardent une copie.
  C'est le seul endroit où ces fichiers sortent de chez moi — choix assumé pour
  le confort, mais à ne pas oublier.
- **Whitelist obligatoire sur le `chat_id`**, vérifiée à chaque message. Sinon
  n'importe qui connaissant le nom du bot peut demander mon permis.
- L'API Bot plafonne le téléchargement à **20 Mo par fichier**.

## État

Rien n'est codé. Le dossier existe pour tenir les décisions ci-dessus.
