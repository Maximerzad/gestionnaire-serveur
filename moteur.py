"""Moteur d'analyse : parcours, hachage, cache, détection. Aucune dépendance UI."""

import os
import sys
import re
import time
import shutil
import sqlite3
import hashlib
import difflib
import tempfile
import threading
import unicodedata
from collections import defaultdict, Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from queue import Queue
from datetime import datetime

try:
    from send2trash import send2trash
except ImportError:
    send2trash = None

PARTIAL_CHUNK = 65536          # 64 Ko — hash partiel (début + fin de fichier)
CHUNK_COMPLET = 1024 * 1024    # 1 Mo par lecture — limite les allers-retours SMB
THREADS_SCAN = 16
THREADS_PARTIEL = 32           # petits accès, latence réseau à masquer
THREADS_COMPLET = 12           # gros volumes, le débit sature vite
AGE_MAX_CACHE_JOURS = 90

if getattr(sys, "frozen", False):
    APP_DIR = os.path.dirname(sys.executable)
else:
    APP_DIR = os.path.dirname(os.path.abspath(__file__))


def _dossier_donnees():
    base = os.environ.get("LOCALAPPDATA")
    if not base:
        base = os.path.join(os.path.expanduser("~"), ".local", "share")
    dossier = os.path.join(base, "GestionnaireServeur")
    try:
        os.makedirs(dossier, exist_ok=True)
        temoin = os.path.join(dossier, ".ecriture")
        with open(temoin, "w") as f:
            f.write("ok")
        os.remove(temoin)
        return dossier
    except OSError:
        return tempfile.gettempdir()


DATA_DIR = _dossier_donnees()
CACHE_PATH = os.path.join(DATA_DIR, "hash_cache.db")
JOURNAL_PATH = os.path.join(DATA_DIR, "journal_suppressions.txt")


def chemin_long(chemin):
    """Préfixe \\\\?\\ pour dépasser la limite Windows de 260 caractères."""
    if os.name != "nt" or len(chemin) < 248 or chemin.startswith("\\\\?\\"):
        return chemin
    chemin = os.path.abspath(chemin)
    if chemin.startswith("\\\\"):
        return "\\\\?\\UNC\\" + chemin[2:]
    return "\\\\?\\" + chemin


def taille_lisible(octets):
    for unite in ["o", "Ko", "Mo", "Go"]:
        if octets < 1024:
            return f"{octets:.1f} {unite}"
        octets /= 1024
    return f"{octets:.1f} To"


def formater_duree(secondes):
    if secondes < 60:
        return f"~{int(secondes)} sec"
    elif secondes < 3600:
        m = int(secondes / 60)
        s = int(secondes % 60)
        return f"~{m} min {s} sec" if s else f"~{m} min"
    else:
        h = int(secondes / 3600)
        m = int((secondes % 3600) / 60)
        return f"~{h}h {m} min"


def est_chemin_reseau(chemin):
    return chemin.startswith("//") or chemin.startswith("\\\\")


def logger_suppression(chemin, octets, type_elem):
    try:
        with open(JOURNAL_PATH, "a", encoding="utf-8") as f:
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            f.write(f"{now} | {type_elem} | {octets} octets | {chemin}\n")
    except Exception:
        pass


def supprimer_element(chemin, type_elem):
    """Envoie à la corbeille si possible. Les lecteurs réseau n'ont pas de
    corbeille : repli sur la suppression définitive. Retourne True si
    l'élément est parti à la corbeille."""
    if send2trash is not None:
        try:
            send2trash(os.path.normpath(chemin))
            return True
        except Exception:
            pass
    cible = chemin_long(chemin)
    if type_elem == "dossier":
        shutil.rmtree(cible)
    else:
        os.remove(cible)
    return False


# ── Hachage ─────────────────────────────────────────────────────────────────

def _nouveau_hash():
    return hashlib.blake2b(digest_size=16)


def hash_fichier(path):
    h = _nouveau_hash()
    try:
        with open(chemin_long(path), "rb") as f:
            while bloc := f.read(CHUNK_COMPLET):
                h.update(bloc)
        return h.hexdigest()
    except Exception:
        return None


def hash_fichier_partiel(path, taille_fichier):
    """Premiers + derniers 64 Ko seulement. Pour un fichier ≤ 64 Ko,
    équivaut au hash complet."""
    h = _nouveau_hash()
    try:
        with open(chemin_long(path), "rb") as f:
            h.update(f.read(PARTIAL_CHUNK))
            if taille_fichier > PARTIAL_CHUNK * 2:
                f.seek(-PARTIAL_CHUNK, 2)
                h.update(f.read(PARTIAL_CHUNK))
        return h.hexdigest()
    except Exception:
        return None


class HashCache:
    """Cache des hashs indexé par (chemin, taille, mtime), stocké dans
    %LOCALAPPDATA%. Commit périodique pour survivre aux interruptions,
    purge des entrées non vues depuis 90 jours."""

    COMMIT_TOUTES_LES = 500

    def __init__(self, path=CACHE_PATH):
        self._lock = threading.Lock()
        self._depuis_commit = 0
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute("PRAGMA busy_timeout=15000")
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS cache "
            "(path TEXT PRIMARY KEY, mtime REAL, size INTEGER, hash TEXT, vu REAL)"
        )
        self._conn.commit()

    def get(self, path, mtime, size):
        with self._lock:
            row = self._conn.execute(
                "SELECT hash FROM cache WHERE path=? AND mtime=? AND size=?",
                (path, mtime, size)
            ).fetchone()
            if row:
                self._conn.execute("UPDATE cache SET vu=? WHERE path=?",
                                   (time.time(), path))
        return row[0] if row else None

    def set(self, path, mtime, size, hash_val):
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO cache VALUES (?,?,?,?,?)",
                (path, mtime, size, hash_val, time.time())
            )
            self._depuis_commit += 1
            if self._depuis_commit >= self.COMMIT_TOUTES_LES:
                self._conn.commit()
                self._depuis_commit = 0

    def commit(self):
        with self._lock:
            self._conn.commit()
            self._depuis_commit = 0

    def purger_anciens(self):
        limite = time.time() - AGE_MAX_CACHE_JOURS * 86400
        with self._lock:
            self._conn.execute(
                "DELETE FROM cache WHERE vu IS NULL OR vu < ?", (limite,)
            )
            self._conn.commit()
            self._conn.execute("VACUUM")


# ── Parcours du serveur ─────────────────────────────────────────────────────

def collecter_metadonnees(racines, stop_flag):
    """Parcours parallèle avec scandir. Retourne la liste brute
    (chemin, taille, mtime) de tous les fichiers, l'état partagé,
    la file et les threads (à terminer avec put(None))."""
    file_attente = Queue()
    fichiers = []
    verrou = threading.Lock()
    etat = {"restants": len(racines), "inaccessibles": 0}

    def explorer():
        while True:
            dossier = file_attente.get()
            if dossier is None:
                return
            try:
                if not stop_flag():
                    with os.scandir(dossier) as it:
                        for entree in it:
                            try:
                                if entree.is_dir(follow_symlinks=False):
                                    with verrou:
                                        etat["restants"] += 1
                                    file_attente.put(entree.path)
                                elif entree.is_file(follow_symlinks=False):
                                    st = entree.stat(follow_symlinks=False)
                                    with verrou:
                                        fichiers.append(
                                            (entree.path, st.st_size, st.st_mtime))
                            except Exception:
                                with verrou:
                                    etat["inaccessibles"] += 1
            except Exception:
                with verrou:
                    etat["inaccessibles"] += 1
            finally:
                with verrou:
                    etat["restants"] -= 1

    for r in racines:
        file_attente.put(r)
    explorateurs = [threading.Thread(target=explorer, daemon=True)
                    for _ in range(THREADS_SCAN)]
    for t in explorateurs:
        t.start()
    return fichiers, etat, file_attente, explorateurs


def terminer_collecte(file_attente, explorateurs):
    for _ in explorateurs:
        file_attente.put(None)


def filtrer_fichiers(bruts, exts_ignorees, taille_min):
    return [
        (fp, taille, mtime) for fp, taille, mtime in bruts
        if taille >= taille_min
        and os.path.splitext(fp)[1].lower() not in exts_ignorees
    ]


# ── Détection des doublons exacts ───────────────────────────────────────────

def detecter_doublons_exacts(fichiers, cache, maj, stop_flag):
    """Pipeline en 3 étages : taille → hash partiel → hash complet.
    `maj(fraction, texte)` est appelé au plus toutes les 0,15 s.
    Retourne (groupes, nb_inaccessibles)."""
    inaccessibles = [0]
    dernier_maj = [0.0]

    def signaler(frac, texte):
        if time.time() - dernier_maj[0] > 0.15:
            maj(frac, texte)
            dernier_maj[0] = time.time()

    # Étage 1 : éliminer les tailles uniques
    compte = Counter(taille for _, taille, _ in fichiers)
    candidats = [f for f in fichiers if compte[f[1]] >= 2]
    maj(0.08, f"{len(fichiers) - len(candidats):,} fichiers éliminés (taille unique)"
              f", {len(candidats):,} candidats…")

    # Étage 2 : hash partiel, groupé par (taille, hash).
    # Pour les fichiers ≤ 64 Ko le hash partiel vaut hash complet → cache.
    def _hash_partiel(fp, taille, mtime):
        if taille <= PARTIAL_CHUNK:
            h = cache.get(fp, mtime, taille)
            if h is None:
                h = hash_fichier_partiel(fp, taille)
                if h:
                    cache.set(fp, mtime, taille, h)
            return h
        return hash_fichier_partiel(fp, taille)

    partiels = defaultdict(list)
    total_p = len(candidats)
    fait_p = 0
    verrou = threading.Lock()
    with ThreadPoolExecutor(max_workers=min(THREADS_PARTIEL, max(1, total_p))) as pool:
        futures = {
            pool.submit(_hash_partiel, fp, taille, mtime): (fp, taille, mtime)
            for fp, taille, mtime in candidats
        }
        for future in as_completed(futures):
            if stop_flag():
                pool.shutdown(wait=False, cancel_futures=True)
                return None, inaccessibles[0]
            fp, taille, mtime = futures[future]
            h = future.result()
            with verrou:
                fait_p += 1
                if h:
                    partiels[(taille, h)].append((fp, taille, mtime))
                else:
                    inaccessibles[0] += 1
            signaler(0.08 + (fait_p / max(total_p, 1)) * 0.37,
                     f"Pré-analyse : {os.path.basename(fp)}")

    # Étage 3 : hash complet des groupes restants. Les fichiers ≤ 64 Ko
    # sont déjà intégralement hashés — pas de relecture.
    complets = defaultdict(list)
    a_verifier = []
    for (taille, h), groupe in partiels.items():
        if len(groupe) < 2:
            continue
        if taille <= PARTIAL_CHUNK:
            complets[(taille, "p", h)] = [(fp, taille) for fp, taille, _ in groupe]
        else:
            a_verifier.extend(groupe)

    maj(0.45, f"{len(a_verifier):,} fichiers à vérifier en profondeur…")

    def _hash_complet(fp, taille, mtime):
        h = cache.get(fp, mtime, taille)
        if h is None:
            h = hash_fichier(fp)
            if h:
                cache.set(fp, mtime, taille, h)
        return h

    total_f = len(a_verifier)
    fait_f = 0
    with ThreadPoolExecutor(max_workers=min(THREADS_COMPLET, max(1, total_f))) as pool:
        futures = {
            pool.submit(_hash_complet, fp, taille, mtime): (fp, taille)
            for fp, taille, mtime in a_verifier
        }
        for future in as_completed(futures):
            if stop_flag():
                pool.shutdown(wait=False, cancel_futures=True)
                cache.commit()
                return None, inaccessibles[0]
            fp, taille = futures[future]
            h = future.result()
            with verrou:
                fait_f += 1
                if h:
                    complets[(taille, "c", h)].append((fp, taille))
                else:
                    inaccessibles[0] += 1
            signaler(0.45 + (fait_f / max(total_f, 1)) * 0.50,
                     f"Vérification : {os.path.basename(fp)}")

    cache.commit()

    groupes = []
    for groupe in complets.values():
        if len(groupe) >= 2:
            groupes.append({
                "type": "fichier",
                "chemins": [fp for fp, _ in groupe],
                "taille": groupe[0][1],
            })
    return groupes, inaccessibles[0]


# ── Dossiers identiques (métadonnées, en mémoire) ───────────────────────────

def grouper_dossiers_en_memoire(fichiers_bruts, racines):
    """Empreinte XOR insensible à l'ordre sur (nom relatif, taille, mtime).
    Zéro lecture disque. Les groupes sont des candidats : la suppression
    passe par la corbeille, donc récupérable."""
    racines_norm = {os.path.normpath(r) for r in racines}
    acc = defaultdict(lambda: [0, 0, 0])

    for fp, taille, mtime in fichiers_bruts:
        courant = os.path.dirname(fp)
        while True:
            cn = os.path.normpath(courant)
            if cn in racines_norm or not any(cn.startswith(rn + os.sep) for rn in racines_norm):
                break
            rel = fp[len(courant) + 1:]
            empreinte = int.from_bytes(
                hashlib.blake2b(f"{rel}|{taille}|{int(mtime)}".encode(),
                                digest_size=16).digest(), "big"
            )
            cellule = acc[courant]
            cellule[0] ^= empreinte
            cellule[1] += 1
            cellule[2] += taille
            parent = os.path.dirname(courant)
            if parent == courant:
                break
            courant = parent

    signatures = defaultdict(list)
    for dossier, (xor, nb, total) in acc.items():
        signatures[(xor, nb, total)].append((dossier, total))

    bruts = [g for g in signatures.values() if len(g) >= 2]
    dupes = {d for g in bruts for d, _ in g}

    def parent_deja_en_double(d):
        p = os.path.dirname(d)
        while p and os.path.normpath(p) not in racines_norm:
            if p in dupes:
                return True
            suivant = os.path.dirname(p)
            if suivant == p:
                return False
            p = suivant
        return False

    groupes = []
    for g in bruts:
        if all(parent_deja_en_double(d) for d, _ in g):
            continue
        groupes.append({
            "type": "dossier",
            "chemins": [d for d, _ in g],
            "taille": g[0][1],
        })
    return groupes


# ── Noms similaires ─────────────────────────────────────────────────────────

def normaliser_nom(nom):
    nom = os.path.splitext(nom)[0]
    nom = unicodedata.normalize("NFD", nom)
    nom = "".join(c for c in nom if unicodedata.category(c) != "Mn")
    nom = nom.lower()
    nom = re.sub(r"[_\-\.]+", " ", nom)
    nom = re.sub(r"\s*\(\d+\)\s*$", "", nom)
    nom = re.sub(r"\s*-?\s*(copy|copie)(\s*\(\d+\))?\s*$", "", nom)
    nom = re.sub(r"\s+v\d+\s*$", "", nom)
    nom = re.sub(r"\s+\d{1,4}\s*$", "", nom)
    return re.sub(r"\s+", " ", nom).strip()


def grouper_par_nom_similaire(tous_fichiers, seuil):
    par_ext = defaultdict(list)
    for fp, taille, _mtime in tous_fichiers:
        ext = os.path.splitext(fp)[1].lower()
        nom_norm = normaliser_nom(os.path.basename(fp))
        if nom_norm:
            par_ext[ext].append((fp, nom_norm, taille))

    def _traiter_extension(fichiers):
        if len(fichiers) < 2:
            return []
        fichiers.sort(key=lambda x: x[1])
        n = len(fichiers)
        fenetre = min(n - 1, 30)

        voisins = defaultdict(set)
        for i in range(n):
            fp1, nom1, _ = fichiers[i]
            l1 = len(nom1)
            for j in range(i + 1, min(i + fenetre + 1, n)):
                fp2, nom2, _ = fichiers[j]
                if min(l1, len(nom2)) / max(l1, len(nom2), 1) < seuil - 0.15:
                    continue
                sm = difflib.SequenceMatcher(None, nom1, nom2)
                if sm.real_quick_ratio() < seuil or sm.quick_ratio() < seuil:
                    continue
                if sm.ratio() >= seuil:
                    voisins[fp1].add(fp2)
                    voisins[fp2].add(fp1)

        groupes_ext = []
        visites = set()
        for fp_dep in voisins:
            if fp_dep in visites:
                continue
            groupe_fps = set()
            pile = [fp_dep]
            while pile:
                fp = pile.pop()
                if fp in visites:
                    continue
                visites.add(fp)
                groupe_fps.add(fp)
                pile.extend(voisins[fp] - visites)
            if len(groupe_fps) < 2:
                continue
            chemins = list(groupe_fps)
            taille = next((t for fp, _, t in fichiers if fp == chemins[0]), 0)
            n1 = normaliser_nom(os.path.basename(chemins[0]))
            n2 = normaliser_nom(os.path.basename(chemins[1]))
            sim_pct = round(difflib.SequenceMatcher(None, n1, n2).ratio() * 100)
            groupes_ext.append({
                "type": "fichier",
                "chemins": chemins,
                "taille": taille,
                "mode": "nom",
                "similarite": sim_pct,
            })
        return groupes_ext

    groupes = []
    nb_workers = min(4, max(1, len(par_ext)))
    with ThreadPoolExecutor(max_workers=nb_workers) as pool:
        futures = [pool.submit(_traiter_extension, list(f)) for f in par_ext.values()]
        for future in as_completed(futures):
            groupes.extend(future.result())
    return groupes
