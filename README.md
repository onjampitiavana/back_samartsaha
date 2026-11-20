# Smart Saha

Flask + Supabase (Auth + Postgres/PostGIS) + Jinja2 + Tailwind.

- `/signin`, `/signup` — member accounts, backed by `user_profiles`.
- `/dashboard` — overview with record counts.
- `/cooperatives` — register a cooperative.
- `/farmers` — register a cooperative member.
- `/parcels` — import a parcel boundary (`.kml`, `.gpkg`, or a zipped
  Shapefile) with geopandas, store it in PostGIS, and see it on a map.
- `/forms` — survey forms. Administrators build forms without code and
  assign them to field agents, who fill them in. Run
  `migrations/20261025_formulaires.sql` once first (see the routes table).

Then, in order, in the Supabase SQL editor: `migrations/20261028_role_hierarchy.sql`
(admin → manager → cooperative) and `migrations/20261029_admin_cooperatives.sql`
(staff page in one query, any number of crops per cooperative, defaults for
`legal_status` / `status`). The app keeps working before the last one, with
slower staff pages and at most three crops.
Finally `migrations/20261030_forms_without_archive.sql`: forms are only
`draft` or `published` (former archived forms go back to published, or to
draft if never published).

Parcels drawn on the map (4.2, managers only): run
`migrations/20261119_parcels_drawn.sql`. `POST /api/parcels/draw` takes
{cooperative_id, farmer_id, nom, geometry} (a GeoJSON Polygon), checks it
(3+ vertices, closed ring, no self-intersection, valid GPS coordinates) and
calls `insert_drawn_parcel`, which applies exactly the file import's rules:
same ownership checks, SRID 4326, 2D, `ST_MakeValid` only when needed,
`area_ha = round(ST_Area(geom::geography) / 10000, 4)`.

Fire map and proximity alerts (4.3): run `migrations/20261118_fire_alerts.sql`.
NASA FIRMS is called by this server only, with `MAP_KEY` from `.env`, for the
bounding box of the registered parcels, at most once every
`FIRE_REFRESH_MINUTES` (default 180), whatever the number of workers (the
lock is `fire_claim_fetch` in Supabase). A background thread does it; with
`FIRE_SCHEDULER=0`, run `flask --app run fires-refresh` from cron instead
(`--force` ignores the interval). Each hotspot within `FIRE_ALERT_RADIUS_KM`
(default 5) of a parcel creates one alert per parcel (never twice), which is
e-mailed to the cooperative's manager, admin and staff. Clients read the
cache: `/api/fires*` (web) and the RPC `get_my_fire_updates` (mobile delta
sync).

Audit Bio (managers, Regulation (EU) 2018/848): run
`migrations/20261122_audits_bio_web.sql` (after `20261121_dashboard_map.sql`).
Routes in `core/routes/audit_bio_api.py` (`/api/audits-bio*`, managers only),
logic in `core/services/audit_bio_service.py`, PDF report in
`core/services/audit_bio_pdf.py`, validation in `core/forms/audit_bio_forms.py`.
- `audits_bio` holds completed, signed audits only (also written by the
  mobile app). Work in progress lives in `audits_bio_drafts` (owner only).
- A recorded audit is locked by a trigger: only the `resolved` /
  `resolved_at` keys of its `non_conformity_items` may change (follow-up).
- RLS: a manager reads / follows up the audits of the parcels he owns,
  whoever recorded them; inserts are limited to parcels in the caller's
  scope; members cannot delete a recorded audit.
- The audited area is computed by PostGIS on insert (`area_ha_audited`);
  campaign and renewal date (`next_audit_due`, +1 year) default from the
  audit date.

Avenant n°1: run `migrations/20261120_avenant1.sql` (after the two above)
and `pip install -r requirements.txt` (reportlab, for the PDF sheets).
- KoboToolbox (admins): each admin enters a personal API key in Paramètres;
  it is checked against Kobo then stored Fernet-encrypted in
  `kobo_connections` (key: `KOBO_ENCRYPTION_KEY`, or derived from
  `SUPABASE_SERVICE_ROLE_KEY`). Importing a form creates a `kobo_forms` row
  and a table `kobo_data.k_<id>` (RLS on, service role only). Syncs run on
  demand (Enquêtes > KoboToolbox), with `flask --app run kobo-sync` from
  cron, or every `KOBO_SYNC_MINUTES`: only submissions whose Kobo `_id` is
  above the last one imported are downloaded, only the question fields, and
  `on conflict (kobo_id) do nothing` prevents duplicates. Each run is logged
  in `kobo_import_logs`. Records can be added, edited and deleted locally;
  a sync never overwrites them.
- geopoint / geotrace / geoshape questions are detected from the form
  definition (several, or guessed from the answers: "uncertain", to
  confirm in the form settings) and converted by `kobo_parse_geo()`. Forms
  with a geometry are optional layers of the Carte menu, for the members of
  the form's cooperative.
- Survey forms get a `geoshape` question (polygon drawn with the parcel
  drawing tool, `geometry(Polygon, 4326)`); point questions are picked on
  a map. The mobile app does not offer geoshape questions yet: don't make
  them required in forms filled on mobile.
- Parcel sheets (managers, Parcelles > Fiches parcellaires): templates in
  `fiche_modeles`; `POST /api/fiches/pdf` builds one PDF for 1 to 200
  parcels from `fiche_parcel_data()` (one SQL call per batch).

Sessions last 30 days for every role (`SESSION_LIFETIME` in
`core/config.py`): the signed Flask cookie is permanent, and the sign-in time
it stores is checked on every request, so the session ends 30 days after
signing in even if the user stays active. The Supabase access token (1 h) is
refreshed in the background with the refresh token kept in that cookie.

Every page is scoped to the signed-in member: each cooperative, farmer, and
parcel row has an `owner` column, enforced by Postgres Row Level Security, so
one member never sees another's records.

## 1. Install

```bash
cd smart-saha
python3 -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

## 2. Configure Supabase

Copy `.env.example` to `.env` and fill in:

```bash
cp .env.example .env
```

- `SUPABASE_URL` / `SUPABASE_KEY` — Project Settings → API → Project URL / `anon` `public` key.
- `SUPABASE_SERVICE_ROLE_KEY` *(optional but recommended)* — Project Settings → API →
  `service_role` key. **Never** send this to the browser; it's only read by `app.py`
  on the server to write the new profile row during signup. This avoids a common
  gotcha: if "Confirm email" is enabled in Supabase Auth, `sign_up()` doesn't return
  a session yet, so the anon key alone can't pass a Row Level Security check.
  With the service key set, signup works either way.

## 3. Make sure `user_profiles` has the right policies

You said the table already exists with `uuid, id, username, full_name, email, mobile`.
Two things to check in the Supabase SQL editor:

```sql
-- uuid should point at the auth user it belongs to
alter table public.user_profiles
  add constraint user_profiles_uuid_fkey
  foreign key (uuid) references auth.users(id) on delete cascade;

alter table public.user_profiles
  add constraint user_profiles_username_key unique (username);

-- Row Level Security: members can only read/update their own row
alter table public.user_profiles enable row level security;

create policy "Members can view their own profile"
  on public.user_profiles for select
  using (auth.uid() = uuid);

create policy "Members can update their own profile"
  on public.user_profiles for update
  using (auth.uid() = uuid);

-- Only needed if you did NOT set SUPABASE_SERVICE_ROLE_KEY above
create policy "Members can insert their own profile"
  on public.user_profiles for insert
  with check (auth.uid() = uuid);
```

## 4. Create the cooperative / farmer / parcel tables

Run everything in `schema.sql` in the Supabase SQL editor. It:

- enables the `postgis` extension,
- creates `cooperatives`, `farmers`, and `parcels` (with a PostGIS `geometry`
  column + spatial index on `parcels`),
- turns on Row Level Security with an `owner = auth.uid()` policy on all
  three tables,
- adds two RPC functions the app calls directly (PostgREST can't insert or
  read a PostGIS `geometry` column as plain JSON, so these do the
  GeoJSON ⇄ geometry conversion in Postgres):
  - `insert_parcel(coop_id, farmer_id_in, geom_geojson)` — inserts one
    parcel and computes its area in hectares.
  - `get_my_parcels()` — returns the signed-in member's parcels with
    geometry as GeoJSON text, for the table and the map.

## 5. Run

```bash
python app.py
```

Visit `http://127.0.0.1:5000` — it redirects to `/signin`.

## Registering a cooperative / farmer / parcel

- **Cooperative** — name, president, contact details, legal status
  (cooperative / group / other), year of creation, status, and up to three
  crops from a fixed list. `owner` is set automatically to whoever is signed
  in; `uuid` is generated by Postgres.
- **Farmer** — tied to one of your cooperatives, with full name, year of
  birth, gender, ID card number, disability flag, marital status, household
  size, and status.
- **Parcel** — pick the cooperative, then the farmer (the dropdown filters
  client-side to that cooperative's members), and attach a boundary file:
  - `.kml`
  - `.gpkg` (GeoPackage)
  - `.zip` containing a Shapefile (`.shp`, `.shx`, `.dbf`, `.prj`)

  `app.py` reads the file with **geopandas**, reprojects it to WGS84
  (EPSG:4326) for storage, estimates a local UTM zone to compute area in
  hectares, and inserts one parcel per shape found in the file via the
  `insert_parcel` RPC. The parcels page renders every stored shape on a
  Leaflet map (OpenStreetMap tiles) with a popup showing the farmer,
  cooperative, and area.

## How signup writes to `user_profiles`

1. `supabase.auth.sign_up(email, password)` creates the row in Supabase's
   built-in `auth.users` table and returns a user `id` (a UUID).
2. The app immediately inserts one row into `public.user_profiles`:
   `uuid = that id`, plus `username`, `full_name`, `email`, `mobile` from the
   form. `id` is left out so your table's own auto-increment/identity fills it in.
3. On sign-in, the app reads `user_profiles` back with `.eq("uuid", user.id)`
   to populate the dashboard.

## Notes

- Passwords are never touched by this app directly — Supabase Auth handles
  hashing and storage.
- Duplicate usernames are caught and shown as a friendly form error (relies
  on the unique constraint above).
- Swap the Tailwind `<script src="https://cdn.tailwindcss.com">` for a
  compiled build before shipping to production.


## --- FRENCH --- ##

# Smart Saha — Documentation technique

Application web de gestion pour une coopérative agricole rurale : comptes
membres, coopératives, producteurs, parcelles géolocalisées, audits de
conformité biologique (export UE), et gestion/export des données.

**Stack** : Flask (Python) · Supabase (PostgreSQL + PostGIS + Auth) · Jinja2 ·
Tailwind CSS · Leaflet.js · GeoPandas / Fiona / GDAL · pyshp · openpyxl

---

## Sommaire

1. [Architecture générale](#1-architecture-générale)
2. [Installation](#2-installation)
3. [Modèle de données](#3-modèle-de-données)
4. [Fonctions RPC (Postgres)](#4-fonctions-rpc-postgres)
5. [Routes Flask](#5-routes-flask)
6. [Modules fonctionnels](#6-modules-fonctionnels)
7. [Templates](#7-templates)
8. [Import et export de fichiers géographiques](#8-import-et-export-de-fichiers-géographiques)
9. [Pièges connus et notes techniques](#9-pièges-connus-et-notes-techniques)
10. [Arborescence du projet](#10-arborescence-du-projet)

---

## 1. Architecture générale

```
Navigateur (Jinja2 + Tailwind + Leaflet)
        │
        ▼
   Flask (app.py)
        │
        ├── Supabase Auth  (inscription / connexion / session)
        ├── Supabase Postgres + PostGIS  (toutes les tables métier)
        └── GeoPandas / Fiona / pyshp  (import et export de fichiers géo)
```

- **Authentification** : gérée entièrement par Supabase Auth. Le token
  d'accès et le refresh token sont stockés dans la session Flask (cookie),
  puis réinjectés dans le client Supabase à chaque requête
  (`get_user_client()`), afin que les règles **Row Level Security (RLS)**
  de Postgres s'appliquent avec l'identité du membre connecté, et non de
  façon anonyme.
- **Isolation des données** : chaque enregistrement (coopérative,
  producteur, parcelle, audit) porte une colonne `owner` (uuid de
  l'utilisateur Supabase). Les policies RLS garantissent qu'un membre ne
  voit et ne modifie que ses propres données.
- **Géodonnées** : les parcelles sont stockées comme géométries PostGIS
  (`geometry(Geometry, 4326)`, WGS84). Comme PostgREST ne sait pas
  lire/écrire une colonne `geometry` directement en JSON, tout passe par
  des fonctions RPC Postgres (`insert_parcel`, `get_my_parcels`) qui font
  la conversion GeoJSON ⇆ geometry côté base de données.

---

## 2. Installation

### 2.1 Dépendances Python

```
Flask==3.0.3
supabase==2.7.4
python-dotenv==1.0.1
geopandas==0.14.4
shapely==2.0.4
fiona==1.9.6
pyproj==3.6.1
pandas
openpyxl==3.1.5
pyshp==3.1.6
```

```bash
python -m pip install -r requirements.txt
```

> Sur Windows, installez toujours avec `python -m pip install ...` (et non
> juste `pip install ...`) pour être certain d'utiliser le `pip` associé à
> l'interpréteur Python qui exécute réellement l'application — un décalage
> entre plusieurs installations Python est la cause la plus fréquente d'un
> `ModuleNotFoundError` après une installation apparemment réussie.

### 2.2 Variables d'environnement (`.env`)

```
SUPABASE_URL=https://VOTRE-PROJET.supabase.co
SUPABASE_KEY=VOTRE-CLE-ANON-PUBLIC

# Optionnel mais recommandé : clé service_role, utilisée UNIQUEMENT côté
# serveur (jamais envoyée au navigateur) pour écrire le profil juste après
# l'inscription, avant même que Supabase délivre une session au membre.
SUPABASE_SERVICE_ROLE_KEY=VOTRE-CLE-SERVICE-ROLE

# Chaîne aléatoire longue, utilisée pour signer le cookie de session Flask
FLASK_SECRET_KEY=une-chaine-aleatoire-longue

# Envoi des identifiants au personnel créé par un administrateur.
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USER=compte@exemple.org
SMTP_PASSWORD=mot-de-passe-d-application
SMTP_FROM=compte@exemple.org
SMTP_SECURITY=starttls   # starttls | ssl | none
MAIL_BACKEND=smtp        # console : e-mail écrit dans les logs (développement)
```

**Création du personnel par un administrateur** (`POST /api/admin/personnel`) :
l'adresse e-mail est d'abord vérifiée (syntaxe puis enregistrement MX/A du
domaine, `core/services/email_check.py`) ; en cas d'échec, rien n'est créé et
l'API répond « Adresse email introuvable ». Un mot de passe de 8 caractères
(minuscule, majuscule, chiffre et caractère spécial, `password_service.py`) est
généré, le compte est créé puis les identifiants sont envoyés par e-mail
(`mail_service.py`). Si le serveur SMTP refuse le destinataire, le compte est
supprimé et la même erreur est renvoyée. Sans SMTP configuré, la création est
refusée avec un message explicite.

### 2.3 Base de données

Exécuter, dans cet ordre, dans l'éditeur SQL de Supabase :

1. **Schéma de base** : tables `cooperative`, `farmers`, `parcels`
   (PostGIS), RLS, fonctions `insert_parcel()` et `get_my_parcels()`.
2. **Audit bio** : table `audits_bio`, fonction `get_my_audits()`.
3. **Statut de campagne bio** : colonnes `bio_campaign_status` sur
   `farmers`/`parcels`, table `bio_campaign_log`, fonctions
   `set_campaign_status()` et `get_campaign_log()`.

> ⚠️ La table des coopératives s'appelle **`cooperative`** (singulier) dans
> ce projet, pas `cooperatives`. Toutes les requêtes Python et les
> fonctions SQL doivent utiliser ce nom exact.

### 2.4 Lancer l'application

```bash
python app.py
```

Par défaut, accessible sur `http://127.0.0.1:5000`, qui redirige vers
`/signin`.

---

## 3. Modèle de données

### `user_profiles`

Créée à l'inscription (`supabase.auth.sign_up()` puis insertion applicative).

| Colonne     | Type | Description                              |
|-------------|------|-------------------------------------------|
| `uuid`      | uuid | = `auth.users.id` du membre               |
| `id`        | —    | identifiant technique (auto-incrémenté)    |
| `username`  | text | identifiant de connexion affiché           |
| `full_name` | text | nom complet                                |
| `email`     | text | adresse e-mail                             |
| `mobile`    | text | numéro de téléphone                        |

### `cooperative`

| Colonne            | Type    | Contraintes / valeurs possibles                     |
|--------------------|---------|------------------------------------------------------|
| `uuid`             | uuid PK | `gen_random_uuid()`                                   |
| `name`              | text    | requis                                                |
| `president_name`    | text    | requis                                                |
| `email`, `mobile`, `address` | text | optionnels                                   |
| `legal_status`      | text    | `cooperative` / `group` / `other`                     |
| `year_of_creation`  | int     | 1900–2100                                             |
| `status`            | text    | `active` / `not_active` / `na`                        |
| `main_crop`, `second_crop`, `third_crop` | text | liste de cultures (voir `CROPS` dans `app.py`) |
| `owner`             | uuid    | membre propriétaire (`auth.users.id`)                 |
| `created_at`        | timestamptz | par défaut `now()`                                |

### `farmers`

| Colonne                | Type    | Contraintes / valeurs possibles                  |
|-------------------------|---------|----------------------------------------------------|
| `uuid`                  | uuid PK | `gen_random_uuid()`                                 |
| `cooperative_id`        | uuid FK | → `cooperative.uuid`                                |
| `full_name`             | text    | requis                                              |
| `year_of_birth`         | int     | 1900–2025                                           |
| `gender`                | text    | `male` / `female` / `other`                         |
| `id_card`               | text    | numéro de pièce d'identité                          |
| `handicap`              | boolean | vit avec un handicap                                |
| `marital_status`        | text    | `single` / `married` / `divorced` / `widowed` / `other` |
| `household_number`      | int     | taille du foyer                                     |
| `status`                | text    | `active` / `not_active` / `na`                      |
| `bio_campaign_status`   | text    | `considered` / `banned` (défaut `considered`)       |
| `owner`, `created_at`   |         | idem `cooperative`                                  |

### `parcels`

| Colonne                | Type                      | Description                                    |
|-------------------------|---------------------------|-------------------------------------------------|
| `uuid`                  | uuid PK                   | `gen_random_uuid()`                              |
| `cooperative_id`        | uuid FK                   | → `cooperative.uuid`                             |
| `farmer_id`             | uuid FK                   | → `farmers.uuid`                                 |
| `geom`                  | `geometry(Geometry,4326)` | géométrie PostGIS, WGS84                         |
| `area_ha`               | numeric                   | surface en hectares, calculée à l'import          |
| `bio_campaign_status`   | text                      | `considered` / `banned` (défaut `considered`)    |
| `owner`, `created_at`   |                           | idem `cooperative`                               |

Index spatial : `gist (geom)`.

### `audits_bio`

Un audit = une visite, toujours liée à une seule parcelle
(`parcel_id → parcels.uuid`). Sections calquées sur le règlement (UE)
2018/848 :

| Groupe                        | Colonnes |
|--------------------------------|----------|
| Identification                | `parcel_id`, `auditor_name`, `audit_date` |
| Historique de conversion       | `conversion_start_date`, `conversion_status` (`not_started`/`in_conversion`/`certified`) |
| Pratiques agricoles             | `inputs_used`, `seed_origin`, `pest_management_method` |
| Prévention des contaminations   | `neighboring_crops`, `buffer_zone_distance_m`, `contamination_risk` (`low`/`medium`/`high`) |
| Traçabilité                     | `parcel_register_kept`, `inputs_register_kept`, `harvest_register_kept`, `sales_register_kept`, `bio_nonbio_separation` |
| Stockage                        | `storage_handling_notes` |
| Constat de conformité            | `non_conformities`, `non_conformity_severity` (`none`/`minor`/`major`/`critical`), `corrective_action_plan`, `corrective_action_deadline` |
| Conclusion                       | `overall_status` (`compliant`/`non_compliant`/`compliant_with_reservations`), `auditor_signature` (PNG base64) |
| Métadonnées                      | `owner`, `created_at` |

### `bio_campaign_log`

Historique complet de chaque changement d'inclusion/exclusion de la
campagne de certification bio en cours (producteur ou parcelle).

| Colonne           | Type | Description                                              |
|--------------------|------|------------------------------------------------------------|
| `uuid`             | uuid PK | |
| `entity_type`      | text | `farmer` ou `parcel` |
| `farmer_id`        | uuid | rempli si `entity_type = 'farmer'`, sinon `NULL` |
| `parcel_id`        | uuid | rempli si `entity_type = 'parcel'`, sinon `NULL` |
| `status`           | text | `considered` / `banned` |
| `reason`           | text | motif du changement (optionnel) |
| `effective_date`   | date | par défaut la date du jour |
| `owner`, `created_at` | | |

Contrainte : exactement un des deux champs `farmer_id` / `parcel_id` doit
être renseigné selon `entity_type`.

---

## 4. Fonctions RPC (Postgres)

Toutes les fonctions ci-dessous sont `SECURITY DEFINER` : elles s'exécutent
avec les droits du propriétaire de la fonction (pour pouvoir écrire dans
des tables aux policies RLS restrictives), mais vérifient **elles-mêmes**
que la ligne cible appartient bien à `auth.uid()` avant d'agir. Le client
Python ne peut donc jamais agir au nom d'un autre membre.

| Fonction | Paramètres | Rôle |
|----------|------------|------|
| `insert_parcel` | `coop_id`, `farmer_id_in`, `geom_geojson` | Insère une parcelle à partir d'une géométrie GeoJSON, calcule `area_ha` automatiquement. |
| `get_my_parcels` | — | Retourne les parcelles du membre connecté, jointes avec le nom de la coopérative et du producteur, géométrie en GeoJSON texte. |
| `get_my_audits` | — | Retourne les audits du membre connecté, joints avec coopérative/producteur/parcelle. |
| `set_campaign_status` | `entity_type_in`, `entity_id_in`, `new_status_in`, `reason_in` | Met à jour `bio_campaign_status` **et** insère la ligne d'historique dans `bio_campaign_log`, dans une seule transaction. |
| `get_campaign_log` | — | Historique des changements de statut de campagne, avec noms joints. |

---

## 5. Routes Flask

| Route | Méthode(s) | Description |
|-------|------------|--------------|
| `/` | GET | Redirige vers `/dashboard` ou `/signin` |
| `/signup` | GET, POST | Création de compte |
| `/signin` | GET, POST | Connexion |
| `/logout` | GET | Déconnexion |
| `/dashboard` | GET | Vue d'ensemble (compteurs coopératives/producteurs/parcelles) |
| `/cooperatives` | GET, POST | Liste + formulaire d'enregistrement d'une coopérative |
| `/farmers` | GET, POST | Liste + formulaire d'enregistrement d'un producteur |
| `/parcels` | GET, POST | Liste + import de parcelle (fichier) + carte Leaflet |
| `/audits` | GET, POST | Liste + formulaire d'audit bio |
| `/data` | GET | Page de gestion des données (édition, export, statut de campagne) |
| `/data/farmers/<uuid>/edit` | POST | Modifie un producteur |
| `/data/parcels/<uuid>/edit` | POST | Réaffecte une parcelle à une autre coopérative/producteur |
| `/data/campaign-status` | POST | Bascule le statut `considered`/`banned` (producteur ou parcelle) |
| `/data/export/parcels.xlsx` | GET | Export tableur des parcelles (avec géométrie en WKT) |
| `/data/export/parcels.kml` | GET | Export KML des parcelles |
| `/data/export/parcels.shp` | GET | Export Shapefile zippé des parcelles |
| `/data/export/farmers.xlsx` | GET | Export tableur des producteurs |
| `/data/export/cooperatives.xlsx` | GET | Export tableur des coopératives |
| `/forms/` | GET | Admin : formulaires de ses coopératives · Agent : formulaires qui lui sont assignés |
| `/forms/new`, `/forms/<id>/edit` | GET, POST | Constructeur de formulaire sans code (admin) — enregistre un brouillon dans `formulaires.fields` |
| `/forms/<id>` | GET | Nombre de réponses, enquêteurs assignés et chaque réponse (agent, questions, réponses) (admin) |
| `/forms/<id>/publish` | POST | Fige les champs en version n+1 et crée la table `form_data.f_<id>_v<n>` (`publish_formulaire`) |
| `/forms/<id>/agents` | POST | Assigne les enquêteurs (table `formulaire_enqueteurs`, n-n) |
| `/forms/<id>/delete` | POST | Supprime un brouillon jamais publié |
| `/forms/<id>/fill` | GET, POST | Saisie d'une réponse (dernière version), tracée avec l'identité de l'enquêteur |
| `/forms/<id>/responses` (`.xlsx`) | GET | Réponses d'une version (agent : les siennes uniquement), export tableur |

---

## 6. Modules fonctionnels

### 6.1 Authentification
Inscription/connexion via Supabase Auth. À l'inscription, une ligne
`user_profiles` est créée (avec la clé `service_role` si configurée, pour
fonctionner même si la confirmation d'e-mail est activée côté Supabase).

### 6.2 Coopératives / Producteurs
Formulaires classiques d'enregistrement avec validation côté serveur,
listés dans un tableau. Chaque producteur est rattaché à une coopérative
du membre connecté.

### 6.3 Parcelles
- **Import de fichier** : `.kml`, `.gpkg`, ou `.zip` (Shapefile). Chaque
  forme du fichier devient une parcelle distincte.
- Reprojection automatique en WGS84 pour le stockage, calcul de la
  superficie via une zone UTM estimée.
- Carte Leaflet affichant toutes les parcelles enregistrées, avec pop-up
  (coopérative, producteur, surface).

### 6.4 Audits bio
Formulaire structuré en 8 sections calquées sur les exigences
d'exportation biologique vers l'Union Européenne (règlement (UE)
2018/848) : identification, historique de conversion, pratiques
agricoles, prévention des contaminations, traçabilité, stockage,
non-conformités, conclusion + signature (pad tactile/souris en `<canvas>`,
encodée en PNG base64). Chaque audit est lié à une parcelle déjà
enregistrée et préremplit ses informations d'identification.

### 6.5 Gestion des données (`/data`)
- **Export** : parcelles en `.kml` / `.shp` (zip) / `.xlsx` ; producteurs
  et coopératives en `.xlsx`.
- **Édition en ligne** : modification des champs d'un producteur, ou
  réaffectation d'une parcelle à une autre coopérative/producteur, via un
  `<details>` dépliable dans le tableau (pas de JavaScript nécessaire).
- **Statut de campagne bio** : chaque producteur et chaque parcelle peut
  être marqué `considered` (pris en compte dans la campagne de
  certification en cours) ou `banned` (exclu), avec un motif optionnel.
  Chaque changement est historisé dans `bio_campaign_log` et consultable
  dans un tableau d'historique en bas de page.

---

## 7. Templates

```
templates/
├── auth_base.html       # Layout écran scindé (branding + formulaire) pour signin/signup
├── signin.html
├── signup.html
├── app_base.html        # Layout avec barre latérale (Overview / Cooperatives / Farmers / Parcels / Bio audits / Data)
├── dashboard.html
├── cooperatives.html
├── farmers.html
├── parcels.html
├── audit.html
└── data_management.html
```

Charte graphique : vert forêt (`#1B4332`) / vert croissance (`#40916C`) /
or récolte (`#C9974D`) / papier (`#FDFCFA`) / encre (`#1F2A24`), police
`Fraunces` pour les titres et `Inter` pour le reste, via Tailwind CSS
(CDN).

---

## 8. Import et export de fichiers géographiques

### Import (`extract_features_from_upload`)
- Formats acceptés : `.kml`, `.gpkg`, `.zip` (Shapefile).
- Utilise **GeoPandas + Fiona/GDAL** pour la lecture.
- Le pilote `KML` de GDAL n'est pas activé par défaut côté lecture : on
  force `fiona.drvsupport.supported_drivers["KML"] = "rwa"` au démarrage
  de l'application.
- Un KML peut contenir plusieurs dossiers = plusieurs "layers" : ils sont
  tous lus et empilés dans un même GeoDataFrame.
- Chaque forme du fichier devient une parcelle, avec calcul de surface via
  une projection UTM estimée automatiquement (`estimate_utm_crs()`).

### Export
Les exports **n'utilisent volontairement pas** `geopandas.to_file()` /
GDAL pour l'écriture — voir la section suivante pour la raison — mais
Python pur :

- **`.xlsx`** : `pandas.DataFrame.to_excel(..., engine="openpyxl")`.
- **`.kml`** : génération manuelle du XML (`build_kml()`), avec échappement
  des caractères spéciaux (`xml.sax.saxutils.escape`).
- **`.shp`** : écriture via **pyshp** (`build_shapefile_zip()`), avec
  orientation correcte des anneaux via `shapely.geometry.polygon.orient()`
  (le format Shapefile exige les anneaux extérieurs en sens horaire et les
  trous en sens antihoraire — l'inverse de la convention GeoJSON/Shapely).

---

## 9. Pièges connus et notes techniques

Cette section documente des bugs réels rencontrés en développement,
spécifiques à Windows, pour éviter de les reproduire.

### 9.1 `unsupported driver: 'LIBKML'`
GeoPandas utilise par défaut le moteur `pyogrio`, qui résout les fichiers
`.kml` vers le pilote GDAL optionnel `LIBKML`. La plupart des builds GDAL
installés via pip ne l'incluent pas. **Solution** : forcer le moteur
`fiona` avec le pilote `KML` (basique, toujours présent) pour la
**lecture** de KML :
```python
gpd.read_file(path, engine="fiona", driver="KML", layer=layer)
```

### 9.2 `unsupported driver: 'KML'` (Fiona)
Fiona n'active pas le pilote `KML` par défaut. Il faut l'activer
explicitement, une fois, au démarrage de l'app (avant tout `import
geopandas`) :
```python
import fiona
fiona.drvsupport.supported_drivers["KML"] = "rwa"
import geopandas as gpd
```

### 9.3 Écriture KML/SHP via GDAL sur Windows : `WinError 267`
`geopandas.to_file(path, driver="KML")` et
`geopandas.to_file(path)` (Shapefile) peuvent lever
`NotADirectoryError: [WinError 267] The directory name is invalid` sur
Windows. **Solution retenue** : ne plus utiliser GDAL du tout pour
l'**écriture** des exports :
- KML : génération XML manuelle (`build_kml()`).
- Shapefile : écriture avec **pyshp**, une bibliothèque pure Python sans
  dépendance à GDAL (`build_shapefile_zip()`).

### 9.4 ⚠️ Piège critique : `send_file()` depuis un `TemporaryDirectory`
**Ne jamais faire** :
```python
with tempfile.TemporaryDirectory() as tmp:
    path = build_something(tmp)
    return send_file(path, ...)   # ❌ dangereux sur Windows
```
À la sortie du `return`, le bloc `with` se termine et **supprime
immédiatement** le dossier temporaire — alors que `send_file` peut encore
être en train de lire/streamer le fichier vers le client. Sur Linux/Mac,
supprimer un fichier ouvert est toléré (le contenu reste accessible tant
que le descripteur est ouvert). **Sur Windows, c'est interdit** : la
suppression échoue avec `WinError 267` (ou parfois `WinError 32`).

**Solution** : lire le fichier en mémoire *avant* la fin du `with`, puis
envoyer les octets déjà chargés :
```python
with tempfile.TemporaryDirectory() as tmp:
    path = build_something(tmp)
    with open(path, "rb") as f:
        data = f.read()
# le dossier est maintenant nettoyé sans handle ouvert - sûr sur Windows
return send_file(io.BytesIO(data), ...)
```
Ce schéma est utilisé dans `export_parcels_shp`.

### 9.5 Dossier temporaire dédié
Un dossier `tmp_exports/` est créé à côté de `app.py`
(`EXPORT_TMP_DIR`) plutôt que d'utiliser le dossier `TEMP` système par
défaut, par prudence (chemins courts 8.3 Windows, caractères spéciaux dans
le profil utilisateur, etc.). Il est nettoyé automatiquement à chaque
requête (`tempfile.TemporaryDirectory(dir=EXPORT_TMP_DIR)`), donc rien n'y
persiste durablement — inutile de le sauvegarder ou de le versionner
(ajoutez-le à `.gitignore`).

### 9.6 Champs Shapefile limités à 10 caractères
Le format DBF (utilisé par Shapefile pour les attributs) tronque les noms
de colonnes à 10 caractères. Les colonnes d'export sont donc nommées
courtes dès le départ (`coop`, `farmer`, `area_ha`, `created`) pour éviter
toute troncation silencieuse.

### 9.7 Windows / environnements Python multiples
`ModuleNotFoundError` après une installation "réussie" signifie presque
toujours que `pip install` et l'exécution de l'app utilisent deux
interpréteurs Python différents. Toujours utiliser :
```
python -m pip install <paquet>
```
depuis le même terminal/environnement (venv ou conda activé) que celui
qui lance `python app.py`.

---

## 10. Arborescence du projet

```
smart-saha/
├── app.py
├── requirements.txt
├── .env                       # non versionné
├── tmp_exports/                # créé automatiquement, non versionné
└── templates/
    ├── auth_base.html
    ├── signin.html
    ├── signup.html
    ├── app_base.html
    ├── dashboard.html
    ├── cooperatives.html
    ├── farmers.html
    ├── parcels.html
    ├── audit.html
    └── data_management.html
```

Scripts SQL à conserver à part (non exécutés par l'application, à lancer
manuellement dans Supabase) :

```
schema.sql                       # cooperative / farmers / parcels + RPC parcelles
schema_audits_bio.sql            # audits_bio + get_my_audits
schema_bio_campaign_status.sql   # bio_campaign_status + bio_campaign_log + RPC
```
Cooperative active / inactive: run `migrations/20261123_cooperative_is_active.sql`
(after every migration above). It adds `cooperative.is_active` (boolean,
default `true`), kept in sync with the old text column `status`
(`active` / `not_active`). An inactive cooperative accepts no new farmer,
parcel, audit bio or staff member (Flask checks + database triggers). A
cooperative can only be deleted when it is inactive and nothing is attached
to it (`cooperative_linked_counts`). Routes:
`PATCH /api/cooperatives/<uuid>/status` {is_active} and
`DELETE /api/cooperatives/<uuid>` (managers and admins).
