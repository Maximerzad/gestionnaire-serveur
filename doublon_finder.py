import customtkinter as ctk
import os
import sys
import hashlib
import threading
import shutil
import csv
import fnmatch
import time
import sqlite3
import difflib
import unicodedata
import re
from collections import defaultdict, Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from tkinter import filedialog, messagebox
import tkinter as tk

ctk.set_appearance_mode("light")
ctk.set_default_color_theme("blue")

BLEU = "#2563EB"
BLEU_HOVER = "#1D4ED8"
ROUGE = "#EF4444"
ROUGE_HOVER = "#DC2626"
ORANGE = "#F59E0B"
VERT = "#10B981"
VERT_HOVER = "#059669"
GRIS_FOND = "#F8FAFC"
GRIS_CARD = "#FFFFFF"
GRIS_BORDURE = "#E2E8F0"
TEXTE_PRINCIPAL = "#0F172A"
TEXTE_SECONDAIRE = "#64748B"

if getattr(sys, "frozen", False):
    APP_DIR = os.path.dirname(sys.executable)
else:
    APP_DIR = os.path.dirname(os.path.abspath(__file__))

JOURNAL_PATH = os.path.join(APP_DIR, "journal_suppressions.txt")
CACHE_PATH   = os.path.join(APP_DIR, "hash_cache.db")
PARTIAL_CHUNK = 65536  # 64 Ko — taille du hash partiel


def hash_fichier(path, chunk=65536):
    h = hashlib.md5()
    try:
        with open(path, "rb") as f:
            while data := f.read(chunk):
                h.update(data)
        return h.hexdigest()
    except Exception:
        return None


def hash_dossier(path):
    h = hashlib.md5()
    try:
        for root, dirs, files in os.walk(path):
            dirs.sort()
            for fname in sorted(files):
                fhash = hash_fichier(os.path.join(root, fname))
                if fhash:
                    h.update(fhash.encode())
        return h.hexdigest()
    except Exception:
        return None


def taille_lisible(octets):
    for unite in ["o", "Ko", "Mo", "Go"]:
        if octets < 1024:
            return f"{octets:.1f} {unite}"
        octets /= 1024
    return f"{octets:.1f} To"


def logger_suppression(chemin, octets, type_elem):
    try:
        with open(JOURNAL_PATH, "a", encoding="utf-8") as f:
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            f.write(f"{now} | {type_elem} | {octets} octets | {chemin}\n")
    except Exception:
        pass


def hash_fichier_partiel(path, taille_fichier):
    """Hash des premiers + derniers 64 Ko seulement — ~100x plus rapide qu'un hash complet."""
    h = hashlib.md5()
    try:
        with open(path, "rb") as f:
            h.update(f.read(PARTIAL_CHUNK))
            if taille_fichier > PARTIAL_CHUNK * 2:
                f.seek(-PARTIAL_CHUNK, 2)
                h.update(f.read(PARTIAL_CHUNK))
        return h.hexdigest()
    except Exception:
        return None


class HashCache:
    """Cache SQLite des hash MD5 indexé par (chemin, taille, mtime).
    Évite de relire les fichiers inchangés lors des scans suivants."""

    def __init__(self, path):
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS cache "
            "(path TEXT PRIMARY KEY, mtime REAL, size INTEGER, hash TEXT)"
        )
        self._conn.commit()

    def get(self, path, mtime, size):
        with self._lock:
            row = self._conn.execute(
                "SELECT hash FROM cache WHERE path=? AND mtime=? AND size=?",
                (path, mtime, size)
            ).fetchone()
        return row[0] if row else None

    def set(self, path, mtime, size, hash_val):
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO cache VALUES (?,?,?,?)",
                (path, mtime, size, hash_val)
            )

    def commit(self):
        with self._lock:
            self._conn.commit()

    def purge_obsoletes(self):
        """Supprime les entrées dont le fichier n'existe plus."""
        with self._lock:
            rows = self._conn.execute("SELECT path FROM cache").fetchall()
        obsoletes = [r[0] for r in rows if not os.path.exists(r[0])]
        if obsoletes:
            with self._lock:
                self._conn.executemany("DELETE FROM cache WHERE path=?",
                                       [(p,) for p in obsoletes])
                self._conn.commit()


def normaliser_nom(nom):
    """Normalise un nom de fichier pour la comparaison : accents, casse, séparateurs, suffixes de copie."""
    nom = os.path.splitext(nom)[0]
    # Supprimer les accents
    nom = unicodedata.normalize("NFD", nom)
    nom = "".join(c for c in nom if unicodedata.category(c) != "Mn")
    nom = nom.lower()
    # Remplacer séparateurs par espace
    nom = re.sub(r"[_\-\.]+", " ", nom)
    # Supprimer suffixes Windows de copie : (1), (2), - Copie, copy, copie…
    nom = re.sub(r"\s*\(\d+\)\s*$", "", nom)
    nom = re.sub(r"\s*-?\s*(copy|copie)(\s*\(\d+\))?\s*$", "", nom)
    # Supprimer versions : v2, v3, _v2, - v2
    nom = re.sub(r"\s+v\d+\s*$", "", nom)
    # Supprimer années ou chiffres isolés en fin : 2023, 2, 3…
    nom = re.sub(r"\s+\d{1,4}\s*$", "", nom)
    return re.sub(r"\s+", " ", nom).strip()


def grouper_par_nom_similaire(tous_fichiers, seuil):
    """
    Regroupe les fichiers dont le nom normalisé est similaire (ratio ≥ seuil).
    Retourne une liste de groupes avec mode='nom' et similarite (0-100).
    """
    par_ext = defaultdict(list)
    for fp, taille, _mtime in tous_fichiers:
        ext = os.path.splitext(fp)[1].lower()
        nom_norm = normaliser_nom(os.path.basename(fp))
        if nom_norm:
            par_ext[ext].append((fp, nom_norm, taille))

    groupes = []

    for fichiers in par_ext.values():
        if len(fichiers) < 2:
            continue
        fichiers.sort(key=lambda x: x[1])
        n = len(fichiers)
        # Fenêtre glissante après tri alphabétique : noms similaires sont proches
        fenetre = min(n - 1, 60)

        # Graphe d'adjacence
        voisins = defaultdict(set)
        for i in range(n):
            fp1, nom1, _ = fichiers[i]
            for j in range(i + 1, min(i + fenetre + 1, n)):
                fp2, nom2, _ = fichiers[j]
                # Rejet rapide si longueurs trop différentes
                if min(len(nom1), len(nom2)) / max(len(nom1), len(nom2), 1) < seuil - 0.1:
                    continue
                sim = difflib.SequenceMatcher(None, nom1, nom2).ratio()
                if sim >= seuil:
                    voisins[fp1].add(fp2)
                    voisins[fp2].add(fp1)

        # Composantes connexes (flood fill)
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
            # Taille du premier fichier du groupe
            taille = next((t for fp, _, t in fichiers if fp == chemins[0]), 0)
            # Similarité représentative entre les deux premiers
            n1 = normaliser_nom(os.path.basename(chemins[0]))
            n2 = normaliser_nom(os.path.basename(chemins[1]))
            sim_pct = round(difflib.SequenceMatcher(None, n1, n2).ratio() * 100)
            groupes.append({
                "type": "fichier",
                "chemins": chemins,
                "taille": taille,
                "mode": "nom",
                "similarite": sim_pct,
            })

    return groupes


def _formater_duree(secondes):
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


class Tooltip:
    def __init__(self, widget, texte):
        self._widget = widget
        self._texte = texte
        self._popup = None
        widget.bind("<Enter>", self._afficher)
        widget.bind("<Leave>", self._masquer)

    def _afficher(self, event):
        x = self._widget.winfo_rootx() + 20
        y = self._widget.winfo_rooty() + self._widget.winfo_height() + 4
        self._popup = tk.Toplevel(self._widget)
        self._popup.overrideredirect(True)
        self._popup.geometry(f"+{x}+{y}")
        tk.Label(
            self._popup, text=self._texte,
            background="#1E293B", foreground="white",
            font=("Segoe UI", 11), padx=10, pady=5,
            wraplength=650, justify="left"
        ).pack()

    def _masquer(self, event):
        if self._popup:
            self._popup.destroy()
            self._popup = None


class DoublonFinder(ctk.CTk):
    PATTERNS_INUTILES = [
        "Thumbs.db", "desktop.ini", ".DS_Store", "ehthumbs.db",
        "*.tmp", "*.log", "*.bak", "~$*",
    ]
    MOTS_SENSIBLES = [
        "mot de passe", "motdepasse", "mdp", "password", "passwd",
        "identifiant", "login", "secret", "credential", " pin", "_pin",
    ]

    def __init__(self):
        super().__init__()
        self.title("Gestionnaire de serveur")
        self.geometry("980x760")
        self.minsize(860, 660)
        self.configure(fg_color=GRIS_FOND)
        self.resizable(True, True)

        self.dossiers_choisis = []   # liste des racines à analyser
        self.resultats = []
        self.cases = []
        self._stop_analyse = False
        self._inaccessibles = 0
        self._debut_analyse = None

        self._build_ui()

    # ── Interface principale ───────────────────────────────────────────────────

    def _build_ui(self):
        header = ctk.CTkFrame(self, fg_color=GRIS_CARD, corner_radius=0)
        header.pack(fill="x")
        inner_h = ctk.CTkFrame(header, fg_color="transparent")
        inner_h.pack(fill="x", padx=24, pady=14)
        ctk.CTkLabel(
            inner_h, text="🗂  Gestionnaire de serveur",
            font=ctk.CTkFont(family="Segoe UI", size=20, weight="bold"),
            text_color=TEXTE_PRINCIPAL
        ).pack(side="left")
        ctk.CTkLabel(
            inner_h, text="Nettoyage et organisation de vos fichiers",
            font=ctk.CTkFont(family="Segoe UI", size=13),
            text_color=TEXTE_SECONDAIRE
        ).pack(side="left", padx=16)
        ctk.CTkButton(
            inner_h, text="📋  Journal",
            font=ctk.CTkFont(family="Segoe UI", size=12),
            fg_color="#F1F5F9", hover_color="#E2E8F0",
            text_color=TEXTE_PRINCIPAL, corner_radius=6, height=32, width=110,
            command=self._ouvrir_journal
        ).pack(side="right")

        self._build_zone_dossiers()

        self.tabs = ctk.CTkTabview(
            self, fg_color=GRIS_FOND,
            segmented_button_fg_color="#E2E8F0",
            segmented_button_selected_color=BLEU,
            segmented_button_selected_hover_color=BLEU_HOVER,
            segmented_button_unselected_color="#E2E8F0",
            segmented_button_unselected_hover_color="#CBD5E1",
            text_color="white",
            text_color_disabled=TEXTE_SECONDAIRE,
        )
        self.tabs.pack(fill="both", expand=True, padx=24, pady=(10, 10))
        self.tabs.add("🔍  Doublons")
        self.tabs.add("🛠  Outils")
        self._build_tab_doublons(self.tabs.tab("🔍  Doublons"))
        self._build_tab_outils(self.tabs.tab("🛠  Outils"))

    def _build_zone_dossiers(self):
        zone = ctk.CTkFrame(self, fg_color=GRIS_CARD, corner_radius=12,
                            border_width=1, border_color=GRIS_BORDURE)
        zone.pack(fill="x", padx=24, pady=(14, 0))
        inner = ctk.CTkFrame(zone, fg_color="transparent")
        inner.pack(fill="x", padx=20, pady=14)

        # Titre + bouton ajouter
        hdr = ctk.CTkFrame(inner, fg_color="transparent")
        hdr.pack(fill="x", pady=(0, 8))
        ctk.CTkLabel(
            hdr, text="Dossiers à analyser",
            font=ctk.CTkFont(family="Segoe UI", size=13, weight="bold"),
            text_color=TEXTE_PRINCIPAL
        ).pack(side="left")
        ctk.CTkLabel(
            hdr,
            text="Ajoutez plusieurs dossiers pour détecter les doublons entre eux",
            font=ctk.CTkFont(family="Segoe UI", size=11),
            text_color=TEXTE_SECONDAIRE
        ).pack(side="left", padx=12)
        ctk.CTkButton(
            hdr, text="➕  Ajouter un dossier",
            font=ctk.CTkFont(family="Segoe UI", size=12, weight="bold"),
            fg_color=BLEU, hover_color=BLEU_HOVER,
            corner_radius=8, height=34, width=170,
            command=self._ajouter_dossier
        ).pack(side="right")

        # Liste des dossiers sélectionnés
        self._liste_frame = ctk.CTkFrame(inner, fg_color="#F8FAFC", corner_radius=8,
                                          border_width=1, border_color=GRIS_BORDURE)
        self._liste_frame.pack(fill="x")
        self._label_vide_dossiers = ctk.CTkLabel(
            self._liste_frame,
            text="Aucun dossier ajouté — cliquez sur ➕ pour commencer",
            font=ctk.CTkFont(family="Segoe UI", size=12),
            text_color=TEXTE_SECONDAIRE
        )
        self._label_vide_dossiers.pack(pady=14)

        # Avertissement réseau (caché par défaut)
        self._label_reseau = ctk.CTkLabel(
            inner,
            text="⚠️  Lecteur réseau détecté — l'analyse peut prendre plusieurs minutes selon la taille du serveur.",
            font=ctk.CTkFont(family="Segoe UI", size=12),
            text_color=ORANGE
        )

    def _ajouter_dossier(self):
        dossier = filedialog.askdirectory(title="Ajouter un dossier à analyser")
        if not dossier:
            return
        if dossier in self.dossiers_choisis:
            messagebox.showinfo("Déjà ajouté", "Ce dossier est déjà dans la liste.")
            return
        self.dossiers_choisis.append(dossier)
        self._rafraichir_liste_dossiers()

    def _supprimer_dossier_liste(self, dossier):
        self.dossiers_choisis.remove(dossier)
        self._rafraichir_liste_dossiers()

    def _rafraichir_liste_dossiers(self):
        for w in self._liste_frame.winfo_children():
            w.destroy()

        if not self.dossiers_choisis:
            self._label_vide_dossiers = ctk.CTkLabel(
                self._liste_frame,
                text="Aucun dossier ajouté — cliquez sur ➕ pour commencer",
                font=ctk.CTkFont(family="Segoe UI", size=12),
                text_color=TEXTE_SECONDAIRE
            )
            self._label_vide_dossiers.pack(pady=14)
            self._label_reseau.pack_forget()
            return

        a_reseau = False
        for dossier in self.dossiers_choisis:
            row = ctk.CTkFrame(self._liste_frame, fg_color="transparent")
            row.pack(fill="x", padx=12, pady=4)

            icone = "🌐" if est_chemin_reseau(dossier) else "📁"
            if est_chemin_reseau(dossier):
                a_reseau = True

            affichage = dossier if len(dossier) < 72 else "..." + dossier[-69:]
            lbl = ctk.CTkLabel(
                row, text=f"{icone}  {affichage}",
                font=ctk.CTkFont(family="Segoe UI", size=12),
                text_color=TEXTE_PRINCIPAL, anchor="w"
            )
            lbl.pack(side="left", fill="x", expand=True)
            if len(dossier) >= 72:
                Tooltip(lbl, dossier)

            ctk.CTkButton(
                row, text="×",
                font=ctk.CTkFont(family="Segoe UI", size=14, weight="bold"),
                fg_color="transparent", hover_color="#FEE2E2",
                text_color=ROUGE, corner_radius=6, height=26, width=30,
                command=lambda d=dossier: self._supprimer_dossier_liste(d)
            ).pack(side="right")

        if a_reseau:
            self._label_reseau.pack(anchor="w", pady=(8, 0))
        else:
            self._label_reseau.pack_forget()

    def _verifier_dossiers(self):
        if not self.dossiers_choisis:
            messagebox.showwarning("Attention", "Ajoutez au moins un dossier à analyser.")
            return False
        return True

    # ── Onglet Doublons ────────────────────────────────────────────────────────

    def _build_tab_doublons(self, parent):
        zone_opts = ctk.CTkFrame(parent, fg_color=GRIS_CARD, corner_radius=12,
                                 border_width=1, border_color=GRIS_BORDURE)
        zone_opts.pack(fill="x", pady=(0, 8))
        inner = ctk.CTkFrame(zone_opts, fg_color="transparent")
        inner.pack(fill="x", padx=20, pady=12)

        row1 = ctk.CTkFrame(inner, fg_color="transparent")
        row1.pack(fill="x")
        ctk.CTkLabel(
            row1, text="Détecter :",
            font=ctk.CTkFont(family="Segoe UI", size=13, weight="bold"),
            text_color=TEXTE_PRINCIPAL
        ).pack(side="left", padx=(0, 12))
        self.check_fichiers = ctk.CTkCheckBox(
            row1, text="Fichiers en double",
            font=ctk.CTkFont(family="Segoe UI", size=13),
            text_color=TEXTE_PRINCIPAL, fg_color=BLEU,
            hover_color=BLEU_HOVER, checkmark_color="white"
        )
        self.check_fichiers.select()
        self.check_fichiers.pack(side="left", padx=(0, 16))
        self.check_dossiers = ctk.CTkCheckBox(
            row1, text="Dossiers en double",
            font=ctk.CTkFont(family="Segoe UI", size=13),
            text_color=TEXTE_PRINCIPAL, fg_color=BLEU,
            hover_color=BLEU_HOVER, checkmark_color="white"
        )
        self.check_dossiers.select()
        self.check_dossiers.pack(side="left", padx=(0, 16))
        self.btn_arreter = ctk.CTkButton(
            row1, text="⏹  Arrêter",
            font=ctk.CTkFont(family="Segoe UI", size=13),
            fg_color=ORANGE, hover_color="#D97706",
            corner_radius=8, height=36, width=120,
            command=self._arreter_analyse
        )
        self.btn_analyser = ctk.CTkButton(
            row1, text="▶  Lancer l'analyse",
            font=ctk.CTkFont(family="Segoe UI", size=13, weight="bold"),
            fg_color=BLEU, hover_color=BLEU_HOVER,
            corner_radius=8, height=36, width=160,
            command=self._lancer_analyse
        )
        self.btn_analyser.pack(side="right")

        row2 = ctk.CTkFrame(inner, fg_color="transparent")
        row2.pack(fill="x", pady=(10, 0))
        ctk.CTkLabel(
            row2, text="Ignorer extensions :",
            font=ctk.CTkFont(family="Segoe UI", size=12),
            text_color=TEXTE_SECONDAIRE
        ).pack(side="left", padx=(0, 6))
        self.entry_extensions = ctk.CTkEntry(
            row2, placeholder_text=".tmp, .log, .db",
            font=ctk.CTkFont(family="Segoe UI", size=12),
            fg_color="#F1F5F9", border_color=GRIS_BORDURE,
            text_color=TEXTE_PRINCIPAL, height=32, width=180
        )
        self.entry_extensions.pack(side="left", padx=(0, 20))
        ctk.CTkLabel(
            row2, text="Taille minimale (Ko) :",
            font=ctk.CTkFont(family="Segoe UI", size=12),
            text_color=TEXTE_SECONDAIRE
        ).pack(side="left", padx=(0, 6))
        self.entry_taille_min = ctk.CTkEntry(
            row2, placeholder_text="1",
            font=ctk.CTkFont(family="Segoe UI", size=12),
            fg_color="#F1F5F9", border_color=GRIS_BORDURE,
            text_color=TEXTE_PRINCIPAL, height=32, width=70
        )
        self.entry_taille_min.pack(side="left")

        row3 = ctk.CTkFrame(inner, fg_color="transparent")
        row3.pack(fill="x", pady=(8, 0))
        self.check_noms_similaires = ctk.CTkCheckBox(
            row3, text="Détecter aussi les noms similaires",
            font=ctk.CTkFont(family="Segoe UI", size=12),
            text_color=TEXTE_PRINCIPAL, fg_color=ORANGE,
            hover_color="#D97706", checkmark_color="white"
        )
        self.check_noms_similaires.pack(side="left", padx=(0, 16))
        ctk.CTkLabel(
            row3, text="Seuil de ressemblance :",
            font=ctk.CTkFont(family="Segoe UI", size=12),
            text_color=TEXTE_SECONDAIRE
        ).pack(side="left", padx=(0, 6))
        self.entry_seuil_sim = ctk.CTkEntry(
            row3, placeholder_text="85",
            font=ctk.CTkFont(family="Segoe UI", size=12),
            fg_color="#F1F5F9", border_color=GRIS_BORDURE,
            text_color=TEXTE_PRINCIPAL, height=32, width=55
        )
        self.entry_seuil_sim.pack(side="left")
        ctk.CTkLabel(
            row3, text="%",
            font=ctk.CTkFont(family="Segoe UI", size=12),
            text_color=TEXTE_SECONDAIRE
        ).pack(side="left", padx=(4, 16))
        ctk.CTkLabel(
            row3,
            text="Ex. : budget 2023.xlsx ≈ Budget_2023 v2.xlsx",
            font=ctk.CTkFont(family="Segoe UI", size=11),
            text_color=TEXTE_SECONDAIRE
        ).pack(side="left")

        self.progress_frame = ctk.CTkFrame(parent, fg_color="transparent")
        self.progress_frame.pack(fill="x", pady=(0, 6))
        self.progress_bar = ctk.CTkProgressBar(
            self.progress_frame, fg_color="#E2E8F0",
            progress_color=BLEU, height=6, corner_radius=3
        )
        self.progress_bar.pack(fill="x")
        self.progress_bar.set(0)
        self.progress_bar.pack_forget()
        self.label_status = ctk.CTkLabel(
            self.progress_frame, text="",
            font=ctk.CTkFont(family="Segoe UI", size=12),
            text_color=TEXTE_SECONDAIRE
        )
        self.label_status.pack(anchor="w", pady=(2, 0))

        zone_res = ctk.CTkFrame(parent, fg_color=GRIS_CARD, corner_radius=12,
                                border_width=1, border_color=GRIS_BORDURE)
        zone_res.pack(fill="both", expand=True)
        hdr = ctk.CTkFrame(zone_res, fg_color="transparent")
        hdr.pack(fill="x", padx=20, pady=(12, 0))
        ctk.CTkLabel(
            hdr, text="Résultats",
            font=ctk.CTkFont(family="Segoe UI", size=14, weight="bold"),
            text_color=TEXTE_PRINCIPAL
        ).pack(side="left")
        self.label_nb = ctk.CTkLabel(
            hdr, text="",
            font=ctk.CTkFont(family="Segoe UI", size=12),
            text_color=TEXTE_SECONDAIRE
        )
        self.label_nb.pack(side="left", padx=10)
        self.label_espace = ctk.CTkLabel(
            hdr, text="",
            font=ctk.CTkFont(family="Segoe UI", size=12, weight="bold"),
            text_color=VERT
        )
        self.label_espace.pack(side="left")
        ctk.CTkButton(
            hdr, text="📄  Exporter CSV",
            font=ctk.CTkFont(family="Segoe UI", size=12),
            fg_color="#F1F5F9", hover_color="#E2E8F0",
            text_color=TEXTE_PRINCIPAL, corner_radius=6, height=30, width=130,
            command=self._exporter_csv
        ).pack(side="right", padx=(6, 0))
        ctk.CTkButton(
            hdr, text="Tout cocher",
            font=ctk.CTkFont(family="Segoe UI", size=12),
            fg_color="#F1F5F9", hover_color="#E2E8F0",
            text_color=TEXTE_PRINCIPAL, corner_radius=6, height=30, width=100,
            command=self._tout_cocher
        ).pack(side="right", padx=(6, 0))
        ctk.CTkButton(
            hdr, text="🗑  Supprimer la sélection",
            font=ctk.CTkFont(family="Segoe UI", size=12, weight="bold"),
            fg_color=ROUGE, hover_color=ROUGE_HOVER,
            corner_radius=6, height=30, width=190,
            command=self._supprimer_selection
        ).pack(side="right")
        self.scroll = ctk.CTkScrollableFrame(
            zone_res, fg_color="transparent",
            scrollbar_button_color="#CBD5E1",
            scrollbar_button_hover_color="#94A3B8"
        )
        self.scroll.pack(fill="both", expand=True, padx=12, pady=(8, 12))
        ctk.CTkLabel(
            self.scroll,
            text="Ajoutez des dossiers et lancez l'analyse pour voir les doublons.",
            font=ctk.CTkFont(family="Segoe UI", size=13),
            text_color=TEXTE_SECONDAIRE
        ).pack(pady=40)

    # ── Onglet Outils ──────────────────────────────────────────────────────────

    def _build_tab_outils(self, parent):
        ctk.CTkLabel(
            parent,
            text="Ajoutez d'abord les dossiers à analyser, puis sélectionnez un outil.",
            font=ctk.CTkFont(family="Segoe UI", size=12),
            text_color=TEXTE_SECONDAIRE
        ).pack(anchor="w", pady=(0, 14))

        grid = ctk.CTkFrame(parent, fg_color="transparent")
        grid.pack(fill="x")

        outils = [
            ("📊  Espace disque",     "Top 10 des sous-dossiers les plus lourds",                self._ouvrir_espace_disque),
            ("🧹  Fichiers inutiles", "Thumbs.db, .tmp, .log, desktop.ini…",                     self._ouvrir_fichiers_inutiles),
            ("📅  Fichiers anciens",  "Non modifiés depuis 6 mois, 1 an, 2 ans ou 3 ans",        self._ouvrir_fichiers_anciens),
            ("🔒  Fichiers sensibles","Noms contenant : mot de passe, mdp, pin…",                self._ouvrir_fichiers_sensibles),
        ]
        for col, (titre, desc, cmd) in enumerate(outils):
            card = ctk.CTkFrame(grid, fg_color=GRIS_CARD, corner_radius=12,
                                border_width=1, border_color=GRIS_BORDURE)
            card.grid(row=0, column=col, padx=(0, 10) if col < 3 else (0, 0), sticky="nsew")
            grid.columnconfigure(col, weight=1)
            inner = ctk.CTkFrame(card, fg_color="transparent")
            inner.pack(fill="both", expand=True, padx=16, pady=16)
            ctk.CTkLabel(
                inner, text=titre,
                font=ctk.CTkFont(family="Segoe UI", size=14, weight="bold"),
                text_color=TEXTE_PRINCIPAL
            ).pack(anchor="w")
            ctk.CTkLabel(
                inner, text=desc,
                font=ctk.CTkFont(family="Segoe UI", size=11),
                text_color=TEXTE_SECONDAIRE, wraplength=170, justify="left"
            ).pack(anchor="w", pady=(4, 14))
            ctk.CTkButton(
                inner, text="Ouvrir",
                font=ctk.CTkFont(family="Segoe UI", size=13),
                fg_color=BLEU, hover_color=BLEU_HOVER,
                corner_radius=8, height=36,
                command=cmd
            ).pack(fill="x")

    # ── Analyse doublons ───────────────────────────────────────────────────────

    def _lancer_analyse(self):
        if not self._verifier_dossiers():
            return
        if not self.check_fichiers.get() and not self.check_dossiers.get():
            messagebox.showwarning("Attention", "Cochez au moins une option.")
            return
        self._stop_analyse = False
        self._inaccessibles = 0
        self._debut_analyse = time.time()
        self.btn_analyser.pack_forget()
        self.btn_arreter.pack(side="right")
        self.progress_bar.pack(fill="x")
        self.progress_bar.set(0)
        self._vider_resultats()
        self.resultats = []
        self.cases = []
        self.label_espace.configure(text="")
        exts_ignorees = self._parse_extensions()
        taille_min = self._parse_taille_min()
        detect_noms = self.check_noms_similaires.get()
        seuil_sim = self._parse_seuil_similarite()
        threading.Thread(
            target=self._analyser,
            args=(exts_ignorees, taille_min, detect_noms, seuil_sim),
            daemon=True
        ).start()

    def _arreter_analyse(self):
        self._stop_analyse = True
        self.label_status.configure(text="Arrêt en cours…")

    def _parse_extensions(self):
        raw = self.entry_extensions.get().strip()
        if not raw:
            return set()
        exts = set()
        for e in raw.split(","):
            e = e.strip().lower()
            if e and not e.startswith("."):
                e = "." + e
            if e:
                exts.add(e)
        return exts

    def _parse_taille_min(self):
        try:
            return max(0, int(self.entry_taille_min.get().strip())) * 1024
        except (ValueError, AttributeError):
            return 1024

    def _parse_seuil_similarite(self):
        try:
            v = int(self.entry_seuil_sim.get().strip())
            return max(50, min(99, v)) / 100
        except (ValueError, AttributeError):
            return 0.85

    def _analyser(self, exts_ignorees, taille_min, detect_noms=False, seuil_sim=0.85):
        cache = HashCache(CACHE_PATH)
        try:
            detect_fichiers = self.check_fichiers.get()
            detect_dossiers = self.check_dossiers.get()
            tous_dossiers = []
            # (chemin, taille, mtime)
            tous_fichiers = []

            # ── Phase 1 : collecte des métadonnées (stat only, pas de lecture) ──
            self._update_status_simple("Phase 1/3 — Collecte des informations…", 0.01)
            dernier_update = time.time()
            for racine in self.dossiers_choisis:
                for root, dirs, files in os.walk(racine):
                    if self._stop_analyse:
                        self.after(0, self._on_arret)
                        return
                    if detect_fichiers:
                        for f in files:
                            ext = os.path.splitext(f)[1].lower()
                            if ext in exts_ignorees:
                                continue
                            fp = os.path.join(root, f)
                            try:
                                st = os.stat(fp)
                                if st.st_size >= taille_min:
                                    tous_fichiers.append((fp, st.st_size, st.st_mtime))
                            except OSError:
                                self._inaccessibles += 1
                    if detect_dossiers:
                        for d in dirs:
                            tous_dossiers.append(os.path.join(root, d))
                    if time.time() - dernier_update > 0.15:
                        nb = len(tous_fichiers)
                        self._update_status_simple(
                            f"Phase 1/3 — {nb:,} fichiers recensés…", 0.01
                        )
                        dernier_update = time.time()

            doublons = []

            # ── Phase 2 : filtre taille + hash partiel ──────────────────────────
            if detect_fichiers and tous_fichiers:
                # Éliminer d'emblée les fichiers de taille unique
                compte = Counter(taille for _, taille, _ in tous_fichiers)
                candidats = [(fp, t, mt) for fp, t, mt in tous_fichiers if compte[t] >= 2]
                nb_elimines = len(tous_fichiers) - len(candidats)
                self._update_status_simple(
                    f"Phase 2/3 — {nb_elimines:,} fichiers éliminés (taille unique)"
                    f", {len(candidats):,} candidats à analyser…",
                    0.08
                )

                # Hash partiel sur les candidats — parallélisé
                hashes_partiels = defaultdict(list)
                total_p = len(candidats)
                self._debut_analyse = time.time()
                nb_workers = min(8, max(1, total_p))
                done_p = 0
                lock_p = threading.Lock()
                with ThreadPoolExecutor(max_workers=nb_workers) as pool:
                    futures_p = {
                        pool.submit(hash_fichier_partiel, fp, taille): (fp, taille, mtime)
                        for fp, taille, mtime in candidats
                    }
                    for future in as_completed(futures_p):
                        if self._stop_analyse:
                            pool.shutdown(wait=False, cancel_futures=True)
                            self.after(0, self._on_arret)
                            return
                        fp, taille, mtime = futures_p[future]
                        h = future.result()
                        with lock_p:
                            done_p += 1
                            if h:
                                hashes_partiels[h].append((fp, taille, mtime))
                            else:
                                self._inaccessibles += 1
                        if time.time() - dernier_update > 0.15:
                            self._update_status(
                                f"Pré-analyse : {os.path.basename(fp)}",
                                0.08 + (done_p / total_p) * 0.37
                            )
                            dernier_update = time.time()

                # Ne conserver que les groupes où le hash partiel correspond à 2+ fichiers
                vrais_candidats = [
                    item
                    for groupe in hashes_partiels.values()
                    if len(groupe) >= 2
                    for item in groupe
                ]
                nb_filtres = len(candidats) - len(vrais_candidats)
                self._update_status_simple(
                    f"Phase 3/3 — {nb_filtres:,} nouveaux éliminés"
                    f", {len(vrais_candidats):,} fichiers à vérifier en profondeur…",
                    0.45
                )

                # ── Phase 3 : hash complet + cache — parallélisé ────────────────
                hashes_complets = defaultdict(list)
                total_f = len(vrais_candidats)
                self._debut_analyse = time.time()

                def _hash_avec_cache(fp, taille, mtime):
                    h = cache.get(fp, mtime, taille)
                    if h is None:
                        h = hash_fichier(fp)
                        if h:
                            cache.set(fp, mtime, taille, h)
                    return h

                done_f = 0
                lock_f = threading.Lock()
                with ThreadPoolExecutor(max_workers=min(8, max(1, total_f))) as pool:
                    futures_f = {
                        pool.submit(_hash_avec_cache, fp, taille, mtime): (fp, taille)
                        for fp, taille, mtime in vrais_candidats
                    }
                    for future in as_completed(futures_f):
                        if self._stop_analyse:
                            pool.shutdown(wait=False, cancel_futures=True)
                            self.after(0, self._on_arret)
                            return
                        fp, taille = futures_f[future]
                        h = future.result()
                        with lock_f:
                            done_f += 1
                            if h:
                                hashes_complets[h].append((fp, taille))
                            else:
                                self._inaccessibles += 1
                        if time.time() - dernier_update > 0.15:
                            self._update_status(
                                f"Vérification : {os.path.basename(fp)}",
                                0.45 + (done_f / max(total_f, 1)) * 0.50
                            )
                            dernier_update = time.time()

                cache.commit()

                for groupe in hashes_complets.values():
                    if len(groupe) >= 2:
                        chemins = [fp for fp, _ in groupe]
                        taille  = groupe[0][1]
                        doublons.append({"type": "fichier", "chemins": chemins, "taille": taille})

            # ── Dossiers (inchangé, pas de cache pertinent) ─────────────────────
            if detect_dossiers and not self._stop_analyse:
                hashes = defaultdict(list)
                total_d = len(tous_dossiers)
                self._debut_analyse = time.time()
                for i, dp in enumerate(tous_dossiers):
                    if self._stop_analyse:
                        self.after(0, self._on_arret)
                        return
                    if time.time() - dernier_update > 0.15:
                        self._update_status(
                            f"Dossier : {os.path.basename(dp)}",
                            0.95 + (i / max(total_d, 1)) * 0.04
                        )
                        dernier_update = time.time()
                    h = hash_dossier(dp)
                    if h:
                        hashes[h].append(dp)
                for chemins in hashes.values():
                    if len(chemins) > 1:
                        try:
                            taille = sum(
                                os.path.getsize(os.path.join(r, f))
                                for r, _, fs in os.walk(chemins[0]) for f in fs
                            )
                        except Exception:
                            taille = 0
                        doublons.append({"type": "dossier", "chemins": chemins, "taille": taille})

            # ── Phase 4 : similarité de noms (optionnel) ────────────────────────
            if detect_noms and tous_fichiers and not self._stop_analyse:
                self._update_status_simple(
                    f"Phase 4/4 — Recherche de noms similaires (seuil {round(seuil_sim*100)}%)…",
                    0.97
                )
                # Exclure les fichiers déjà identifiés comme doublons exacts
                chemins_exacts = {c for g in doublons if g.get("mode") != "nom"
                                  for c in g["chemins"]}
                fichiers_pour_noms = [
                    (fp, t, mt) for fp, t, mt in tous_fichiers
                    if fp not in chemins_exacts
                ]
                groupes_nom = grouper_par_nom_similaire(fichiers_pour_noms, seuil_sim)
                doublons.extend(groupes_nom)

            self.resultats = doublons
            self.after(0, self._afficher_resultats)
        except Exception as e:
            self.after(0, lambda: messagebox.showerror("Erreur", str(e)))
            self.after(0, self._reset_btn)

    def _on_arret(self):
        self.label_status.configure(text="Analyse interrompue.")
        self._reset_btn()

    def _update_status_simple(self, texte, progression):
        self.after(0, lambda m=texte: self.label_status.configure(text=m))
        self.after(0, lambda p=progression: self.progress_bar.set(p))

    def _update_status(self, texte, progression):
        pct = int(progression * 100)
        eta_str = ""
        if self._debut_analyse and progression > 0.02:
            elapsed = time.time() - self._debut_analyse
            restant = elapsed / progression * (1 - progression)
            eta_str = f"  ·  {_formater_duree(restant)} restantes"
        msg = f"{pct}%{eta_str}  ·  {texte}"
        self.after(0, lambda m=msg: self.label_status.configure(text=m))
        self.after(0, lambda p=progression: self.progress_bar.set(p))

    def _vider_resultats(self):
        for w in self.scroll.winfo_children():
            w.destroy()

    def _afficher_resultats(self):
        self._vider_resultats()
        self.cases = []
        self.progress_bar.set(1)
        msg = "Analyse terminée ✓"
        if self._inaccessibles:
            msg += f"  —  {self._inaccessibles} fichier(s) inaccessible(s) ignoré(s)"
        self.label_status.configure(text=msg)

        if not self.resultats:
            self.label_nb.configure(text="")
            nb_dossiers = len(self.dossiers_choisis)
            detail = f" dans {nb_dossiers} dossier{'s' if nb_dossiers > 1 else ''}" if nb_dossiers > 1 else ""
            ctk.CTkLabel(
                self.scroll,
                text=f"✅  Aucun doublon trouvé{detail} !",
                font=ctk.CTkFont(family="Segoe UI", size=14),
                text_color=VERT
            ).pack(pady=40)
            self._reset_btn()
            return

        nb_exacts = sum(1 for g in self.resultats if g.get("mode") != "nom")
        nb_noms   = sum(1 for g in self.resultats if g.get("mode") == "nom")
        detail = f"{nb_exacts} doublon(s) exact(s)"
        if nb_noms:
            detail += f"  +  {nb_noms} groupe(s) de noms similaires"
        self.label_nb.configure(text=detail)

        for groupe in self.resultats:
            mode_nom = groupe.get("mode") == "nom"
            nb_chemins = len(groupe["chemins"])

            couleur_bord = "#FCD34D" if mode_nom else GRIS_BORDURE
            couleur_entete = "#FFFBEB" if mode_nom else "#EFF6FF"

            card = ctk.CTkFrame(self.scroll, fg_color=GRIS_CARD, corner_radius=10,
                                border_width=1, border_color=couleur_bord)
            card.pack(fill="x", pady=(0, 10))

            # ── En-tête du groupe ────────────────────────────────────────────
            entete = ctk.CTkFrame(card, fg_color=couleur_entete, corner_radius=8)
            entete.pack(fill="x", padx=8, pady=(8, 4))
            inner_e = ctk.CTkFrame(entete, fg_color="transparent")
            inner_e.pack(fill="x", padx=14, pady=10)

            if mode_nom:
                n1 = os.path.basename(groupe["chemins"][0])
                n2 = os.path.basename(groupe["chemins"][1]) if nb_chemins > 1 else ""
                titre = f"⚠️  Noms similaires à {groupe.get('similarite', '?')}%"
                sous = f"{n1}  ≈  {n2}" if n2 else n1
                couleur_sous = ORANGE
            else:
                nom_rep = os.path.basename(groupe["chemins"][0])
                emoji = "📄" if groupe["type"] == "fichier" else "📁"
                economie = taille_lisible(groupe["taille"] * (nb_chemins - 1))
                titre = f"{emoji}  {nom_rep}"
                sous = (f"{nb_chemins} copies identiques  ·  {taille_lisible(groupe['taille'])} chacune"
                        f"  →  💾 {economie} récupérables")
                couleur_sous = BLEU

            ctk.CTkLabel(
                inner_e, text=titre,
                font=ctk.CTkFont(family="Segoe UI", size=14, weight="bold"),
                text_color=TEXTE_PRINCIPAL, anchor="w"
            ).pack(anchor="w")
            ctk.CTkLabel(
                inner_e, text=sous,
                font=ctk.CTkFont(family="Segoe UI", size=11),
                text_color=couleur_sous, anchor="w"
            ).pack(anchor="w", pady=(3, 0))

            # ── Ligne de séparation ──────────────────────────────────────────
            ctk.CTkFrame(card, fg_color=couleur_bord, height=1).pack(fill="x", padx=8)

            # ── Fichiers du groupe ───────────────────────────────────────────
            cases_groupe = []
            for j, chemin in enumerate(groupe["chemins"]):
                try:
                    st = os.stat(chemin)
                    taille_ind = taille_lisible(st.st_size)
                    mtime_str = datetime.fromtimestamp(st.st_mtime).strftime("%d/%m/%Y")
                except OSError:
                    taille_ind = "—"
                    mtime_str = "—"

                is_first = (j == 0)
                bg_row = "#F0FDF4" if (is_first and not mode_nom) else "transparent"

                row = ctk.CTkFrame(card, fg_color=bg_row, corner_radius=6)
                row.pack(fill="x", padx=8, pady=(0, 2))
                inner_r = ctk.CTkFrame(row, fg_color="transparent")
                inner_r.pack(fill="x", padx=12, pady=8)

                # Colonne gauche : badge ou case à cocher
                col_left = ctk.CTkFrame(inner_r, fg_color="transparent", width=110)
                col_left.pack(side="left", fill="y")
                col_left.pack_propagate(False)

                if mode_nom:
                    var = ctk.BooleanVar(value=False)
                    var.trace_add("write", lambda *_: self._update_espace_recuperable())
                    ctk.CTkCheckBox(
                        col_left, text="Supprimer", variable=var,
                        fg_color=ROUGE, hover_color=ROUGE_HOVER,
                        checkmark_color="white", width=18,
                        font=ctk.CTkFont(family="Segoe UI", size=11),
                        text_color=TEXTE_SECONDAIRE
                    ).pack(anchor="w", pady=(4, 0))
                    cases_groupe.append((var, chemin, groupe["type"], groupe["taille"]))
                elif is_first:
                    badge = ctk.CTkLabel(
                        col_left, text="✅  Conserver",
                        font=ctk.CTkFont(family="Segoe UI", size=11, weight="bold"),
                        text_color=VERT, fg_color="#DCFCE7",
                        corner_radius=6, padx=8, pady=3
                    )
                    badge.pack(anchor="w")
                else:
                    var = ctk.BooleanVar(value=False)
                    var.trace_add("write", lambda *_: self._update_espace_recuperable())
                    ctk.CTkCheckBox(
                        col_left, text="Supprimer", variable=var,
                        fg_color=ROUGE, hover_color=ROUGE_HOVER,
                        checkmark_color="white", width=18,
                        font=ctk.CTkFont(family="Segoe UI", size=11),
                        text_color=ROUGE
                    ).pack(anchor="w", pady=(4, 0))
                    cases_groupe.append((var, chemin, groupe["type"], groupe["taille"]))

                # Colonne centrale : nom + chemin
                col_mid = ctk.CTkFrame(inner_r, fg_color="transparent")
                col_mid.pack(side="left", fill="x", expand=True, padx=(10, 8))

                nom = os.path.basename(chemin)
                chemin_parent = os.path.dirname(chemin)
                aff_parent = chemin_parent if len(chemin_parent) < 68 else "..." + chemin_parent[-65:]

                ctk.CTkLabel(
                    col_mid, text=nom,
                    font=ctk.CTkFont(family="Segoe UI", size=13, weight="bold"),
                    text_color=TEXTE_PRINCIPAL, anchor="w"
                ).pack(anchor="w")
                lbl_p = ctk.CTkLabel(
                    col_mid, text=aff_parent,
                    font=ctk.CTkFont(family="Segoe UI", size=11),
                    text_color=TEXTE_SECONDAIRE, anchor="w"
                )
                lbl_p.pack(anchor="w", pady=(1, 0))
                if len(chemin_parent) >= 68:
                    Tooltip(lbl_p, chemin_parent)

                # Colonne droite : taille + date
                col_right = ctk.CTkFrame(inner_r, fg_color="transparent")
                col_right.pack(side="right")
                ctk.CTkLabel(
                    col_right, text=taille_ind,
                    font=ctk.CTkFont(family="Segoe UI", size=12, weight="bold"),
                    text_color=TEXTE_PRINCIPAL
                ).pack(anchor="e")
                ctk.CTkLabel(
                    col_right, text=f"modifié le {mtime_str}",
                    font=ctk.CTkFont(family="Segoe UI", size=10),
                    text_color=TEXTE_SECONDAIRE
                ).pack(anchor="e", pady=(2, 0))

            if mode_nom:
                ctk.CTkLabel(
                    card,
                    text="⚠️  Contenus potentiellement différents — vérifiez avant de supprimer",
                    font=ctk.CTkFont(family="Segoe UI", size=11),
                    text_color=ORANGE
                ).pack(anchor="w", padx=20, pady=(4, 10))

            self.cases.extend(cases_groupe)

        self._reset_btn()
        self._update_espace_recuperable()

    def _update_espace_recuperable(self):
        total = sum(taille for var, _, _, taille in self.cases if var.get())
        self.label_espace.configure(
            text=f"💾  {taille_lisible(total)} récupérables" if total else ""
        )

    def _tout_cocher(self):
        for var, *_ in self.cases:
            var.set(True)

    def _supprimer_selection(self):
        selection = [(c, t, ta) for var, c, t, ta in self.cases if var.get()]
        if not selection:
            messagebox.showinfo("Rien à supprimer", "Cochez d'abord les éléments à supprimer.")
            return
        for chemin, _, _ in selection:
            if self._est_protege(chemin):
                messagebox.showerror(
                    "Protection active",
                    f"Impossible de supprimer un chemin parent ou une racine sélectionnée :\n{chemin}"
                )
                return
        nb = len(selection)
        apercu = "\n".join(f"• {os.path.basename(c)}" for c, _, _ in selection[:10])
        if nb > 10:
            apercu += f"\n… et {nb - 10} autre(s)"
        if not messagebox.askyesno(
            "Confirmer la suppression",
            f"Supprimer {nb} élément{'s' if nb > 1 else ''} définitivement ?\n\n{apercu}\n\nCette action est irréversible."
        ):
            return
        erreurs = []
        for chemin, typ, taille in selection:
            try:
                shutil.rmtree(chemin) if typ == "dossier" else os.remove(chemin)
                logger_suppression(chemin, taille, typ)
            except Exception as e:
                erreurs.append(f"{chemin}: {e}")
        if erreurs:
            messagebox.showerror("Erreurs de suppression", "\n".join(erreurs))
        else:
            messagebox.showinfo("Suppression réussie",
                                f"{nb} élément{'s' if nb > 1 else ''} supprimé{'s' if nb > 1 else ''} avec succès.")
        self._lancer_analyse()

    def _est_protege(self, chemin):
        cible = os.path.normpath(chemin)
        for racine in self.dossiers_choisis:
            r = os.path.normpath(racine)
            if cible == r or r.startswith(cible + os.sep):
                return True
        return False

    def _exporter_csv(self):
        if not self.resultats:
            messagebox.showinfo("Rien à exporter", "Lancez d'abord une analyse.")
            return
        date_str = datetime.now().strftime("%Y-%m-%d")
        chemin = filedialog.asksaveasfilename(
            title="Enregistrer le rapport",
            defaultextension=".csv",
            initialfile=f"rapport_doublons_{date_str}.csv",
            filetypes=[("Fichier CSV", "*.csv")]
        )
        if not chemin:
            return
        coches = {c: var.get() for var, c, _, _ in self.cases}
        with open(chemin, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f, delimiter=";")
            w.writerow(["Type", "Nom", "Chemin complet", "Taille", "Statut"])
            for g in self.resultats:
                for i, c in enumerate(g["chemins"]):
                    statut = "Conserver" if i == 0 else ("À supprimer" if coches.get(c, False) else "Doublon")
                    w.writerow([g["type"].capitalize(), os.path.basename(c), c,
                                 taille_lisible(g["taille"]), statut])
        messagebox.showinfo("Export réussi", f"Rapport enregistré :\n{chemin}")

    def _reset_btn(self):
        self.btn_arreter.pack_forget()
        self.btn_analyser.pack(side="right")
        self.btn_analyser.configure(state="normal", text="▶  Lancer l'analyse")

    # ── Journal ────────────────────────────────────────────────────────────────

    def _ouvrir_journal(self):
        win = ctk.CTkToplevel(self)
        win.title("Journal des suppressions")
        win.geometry("720x500")
        win.configure(fg_color=GRIS_FOND)
        win.grab_set()
        ctk.CTkLabel(
            win, text="📋  Journal des suppressions",
            font=ctk.CTkFont(family="Segoe UI", size=16, weight="bold"),
            text_color=TEXTE_PRINCIPAL
        ).pack(anchor="w", padx=24, pady=(20, 8))
        try:
            with open(JOURNAL_PATH, "r", encoding="utf-8") as f:
                contenu = f.read().strip() or "Aucune suppression enregistrée pour l'instant."
        except FileNotFoundError:
            contenu = "Aucune suppression enregistrée pour l'instant."
        box = ctk.CTkTextbox(
            win, font=ctk.CTkFont(family="Segoe UI", size=12),
            fg_color=GRIS_CARD, text_color=TEXTE_PRINCIPAL, corner_radius=8
        )
        box.pack(fill="both", expand=True, padx=24, pady=(0, 20))
        box.insert("0.0", contenu)
        box.configure(state="disabled")

    # ── Espace disque ──────────────────────────────────────────────────────────

    def _ouvrir_espace_disque(self):
        if not self._verifier_dossiers():
            return
        win = ctk.CTkToplevel(self)
        win.title("Espace disque")
        win.geometry("680x520")
        win.configure(fg_color=GRIS_FOND)
        win.grab_set()
        ctk.CTkLabel(
            win, text="📊  Top 10 — Sous-dossiers les plus lourds",
            font=ctk.CTkFont(family="Segoe UI", size=16, weight="bold"),
            text_color=TEXTE_PRINCIPAL
        ).pack(anchor="w", padx=24, pady=(20, 4))
        lbl_st = ctk.CTkLabel(
            win, text="Calcul en cours…",
            font=ctk.CTkFont(family="Segoe UI", size=12),
            text_color=TEXTE_SECONDAIRE
        )
        lbl_st.pack(anchor="w", padx=24)
        scroll = ctk.CTkScrollableFrame(win, fg_color="transparent")
        scroll.pack(fill="both", expand=True, padx=24, pady=(8, 20))

        def scanner():
            tailles = {}
            try:
                for racine in self.dossiers_choisis:
                    for nom in os.listdir(racine):
                        chemin = os.path.join(racine, nom)
                        if os.path.isdir(chemin):
                            cle = chemin
                            try:
                                tailles[cle] = sum(
                                    os.path.getsize(os.path.join(r, f))
                                    for r, _, fs in os.walk(chemin) for f in fs
                                )
                            except OSError:
                                pass
            except Exception as e:
                win.after(0, lambda: lbl_st.configure(text=f"Erreur : {e}"))
                return
            top = sorted(tailles.items(), key=lambda x: x[1], reverse=True)[:10]
            win.after(0, lambda: afficher(top))

        def afficher(top):
            if not top:
                lbl_st.configure(text="Aucun sous-dossier trouvé.")
                return
            lbl_st.configure(text=f"{len(top)} sous-dossier(s) analysé(s)")
            taille_max = top[0][1] or 1
            for chemin, taille in top:
                card = ctk.CTkFrame(scroll, fg_color=GRIS_CARD, corner_radius=8,
                                    border_width=1, border_color=GRIS_BORDURE)
                card.pack(fill="x", pady=(0, 6))
                inner = ctk.CTkFrame(card, fg_color="transparent")
                inner.pack(fill="x", padx=14, pady=10)
                nom_affiche = chemin if len(chemin) < 60 else "..." + chemin[-57:]
                ctk.CTkLabel(
                    inner, text=f"📁  {nom_affiche}",
                    font=ctk.CTkFont(family="Segoe UI", size=13, weight="bold"),
                    text_color=TEXTE_PRINCIPAL
                ).pack(anchor="w")
                bar = ctk.CTkProgressBar(
                    inner, fg_color="#E2E8F0", progress_color=BLEU,
                    height=8, corner_radius=3
                )
                bar.pack(fill="x", pady=(6, 2))
                bar.set(taille / taille_max)
                ctk.CTkLabel(
                    inner, text=taille_lisible(taille),
                    font=ctk.CTkFont(family="Segoe UI", size=12),
                    text_color=TEXTE_SECONDAIRE
                ).pack(anchor="e")

        threading.Thread(target=scanner, daemon=True).start()

    # ── Fichiers inutiles ──────────────────────────────────────────────────────

    def _ouvrir_fichiers_inutiles(self):
        if not self._verifier_dossiers():
            return
        win = ctk.CTkToplevel(self)
        win.title("Fichiers inutiles")
        win.geometry("760x560")
        win.configure(fg_color=GRIS_FOND)
        win.grab_set()
        ctk.CTkLabel(
            win, text="🧹  Fichiers système inutiles",
            font=ctk.CTkFont(family="Segoe UI", size=16, weight="bold"),
            text_color=TEXTE_PRINCIPAL
        ).pack(anchor="w", padx=24, pady=(20, 4))
        lbl_st = ctk.CTkLabel(
            win, text="Recherche en cours…",
            font=ctk.CTkFont(family="Segoe UI", size=12),
            text_color=TEXTE_SECONDAIRE
        )
        lbl_st.pack(anchor="w", padx=24)
        scroll = ctk.CTkScrollableFrame(win, fg_color="transparent")
        scroll.pack(fill="both", expand=True, padx=24, pady=(8, 0))
        btn_frame = ctk.CTkFrame(win, fg_color="transparent")
        btn_frame.pack(fill="x", padx=24, pady=12)
        cases = []

        def supprimer():
            sel = [c for var, c in cases if var.get()]
            if not sel:
                messagebox.showinfo("Rien sélectionné", "Cochez d'abord des fichiers.")
                return
            nb = len(sel)
            if not messagebox.askyesno("Confirmer", f"Supprimer {nb} fichier(s) définitivement ?"):
                return
            erreurs = []
            for c in sel:
                try:
                    os.remove(c)
                    logger_suppression(c, 0, "fichier inutile")
                except Exception as e:
                    erreurs.append(str(e))
            if erreurs:
                messagebox.showerror("Erreurs", "\n".join(erreurs))
            else:
                messagebox.showinfo("Suppression réussie", f"{nb} fichier(s) supprimé(s).")
            win.destroy()

        ctk.CTkButton(
            btn_frame, text="Tout cocher",
            fg_color="#F1F5F9", hover_color="#E2E8F0",
            text_color=TEXTE_PRINCIPAL, corner_radius=6, height=34, width=120,
            command=lambda: [var.set(True) for var, _ in cases]
        ).pack(side="left", padx=(0, 8))
        ctk.CTkButton(
            btn_frame, text="🗑  Supprimer la sélection",
            fg_color=ROUGE, hover_color=ROUGE_HOVER,
            corner_radius=6, height=34,
            command=supprimer
        ).pack(side="left")

        def scanner():
            trouves = []
            try:
                for racine in self.dossiers_choisis:
                    for root, _, files in os.walk(racine):
                        for fname in files:
                            if any(fnmatch.fnmatch(fname, p) for p in self.PATTERNS_INUTILES):
                                trouves.append(os.path.join(root, fname))
            except Exception:
                pass
            win.after(0, lambda: afficher(trouves))

        def afficher(trouves):
            if not trouves:
                lbl_st.configure(text="✅  Aucun fichier inutile trouvé.")
                return
            lbl_st.configure(text=f"{len(trouves)} fichier(s) inutile(s) trouvé(s)")
            for chemin in trouves:
                row = ctk.CTkFrame(scroll, fg_color=GRIS_CARD, corner_radius=8,
                                   border_width=1, border_color=GRIS_BORDURE)
                row.pack(fill="x", pady=(0, 4))
                inner = ctk.CTkFrame(row, fg_color="transparent")
                inner.pack(fill="x", padx=12, pady=8)
                var = ctk.BooleanVar(value=False)
                ctk.CTkCheckBox(
                    inner, text="", variable=var,
                    fg_color=ROUGE, hover_color=ROUGE_HOVER,
                    checkmark_color="white", width=20
                ).pack(side="left")
                ctk.CTkLabel(
                    inner, text=os.path.basename(chemin),
                    font=ctk.CTkFont(family="Segoe UI", size=13, weight="bold"),
                    text_color=TEXTE_PRINCIPAL
                ).pack(side="left", padx=(8, 4))
                parent = os.path.dirname(chemin)
                affichage = parent if len(parent) < 60 else "..." + parent[-57:]
                lbl_p = ctk.CTkLabel(
                    inner, text=affichage,
                    font=ctk.CTkFont(family="Segoe UI", size=11),
                    text_color=TEXTE_SECONDAIRE
                )
                lbl_p.pack(side="left")
                if len(parent) >= 60:
                    Tooltip(lbl_p, parent)
                cases.append((var, chemin))

        threading.Thread(target=scanner, daemon=True).start()

    # ── Fichiers anciens ───────────────────────────────────────────────────────

    def _ouvrir_fichiers_anciens(self):
        if not self._verifier_dossiers():
            return
        win = ctk.CTkToplevel(self)
        win.title("Fichiers anciens")
        win.geometry("820x600")
        win.configure(fg_color=GRIS_FOND)
        win.grab_set()
        ctk.CTkLabel(
            win, text="📅  Fichiers anciens",
            font=ctk.CTkFont(family="Segoe UI", size=16, weight="bold"),
            text_color=TEXTE_PRINCIPAL
        ).pack(anchor="w", padx=24, pady=(20, 8))

        opts = ctk.CTkFrame(win, fg_color=GRIS_CARD, corner_radius=10,
                            border_width=1, border_color=GRIS_BORDURE)
        opts.pack(fill="x", padx=24, pady=(0, 8))
        inner_o = ctk.CTkFrame(opts, fg_color="transparent")
        inner_o.pack(fill="x", padx=16, pady=12)
        ctk.CTkLabel(
            inner_o, text="Non modifiés depuis :",
            font=ctk.CTkFont(family="Segoe UI", size=13),
            text_color=TEXTE_PRINCIPAL
        ).pack(side="left", padx=(0, 10))
        seuil_var = ctk.StringVar(value="1 an")
        ctk.CTkOptionMenu(
            inner_o, values=["6 mois", "1 an", "2 ans", "3 ans"],
            variable=seuil_var,
            fg_color=BLEU, button_color=BLEU_HOVER,
            dropdown_fg_color=GRIS_CARD, text_color="white",
            font=ctk.CTkFont(family="Segoe UI", size=13),
            width=120, height=34
        ).pack(side="left", padx=(0, 16))
        lbl_st = ctk.CTkLabel(
            inner_o, text="",
            font=ctk.CTkFont(family="Segoe UI", size=12),
            text_color=TEXTE_SECONDAIRE
        )
        lbl_st.pack(side="left")

        scroll = ctk.CTkScrollableFrame(win, fg_color="transparent")
        scroll.pack(fill="both", expand=True, padx=24, pady=(0, 0))
        btn_frame = ctk.CTkFrame(win, fg_color="transparent")
        btn_frame.pack(fill="x", padx=24, pady=12)

        cases = []
        SEUILS = {"6 mois": 183, "1 an": 365, "2 ans": 730, "3 ans": 1095}

        def archiver():
            sel = [(c, ta) for var, c, ta in cases if var.get()]
            if not sel:
                messagebox.showinfo("Rien sélectionné", "Cochez des fichiers à archiver.")
                return
            nb = len(sel)
            nom_arch = f"_Archives_{datetime.now().strftime('%Y-%m')}"
            if not messagebox.askyesno(
                "Confirmer l'archivage",
                f"Déplacer {nb} fichier(s) vers un dossier «{nom_arch}» dans leur dossier d'origine ?"
            ):
                return
            erreurs = []
            for chemin, taille in sel:
                try:
                    dossier_arch = os.path.join(os.path.dirname(chemin), nom_arch)
                    os.makedirs(dossier_arch, exist_ok=True)
                    dest = os.path.join(dossier_arch, os.path.basename(chemin))
                    if os.path.exists(dest):
                        base, ext = os.path.splitext(os.path.basename(chemin))
                        dest = os.path.join(dossier_arch, f"{base}_{int(datetime.now().timestamp())}{ext}")
                    shutil.move(chemin, dest)
                    logger_suppression(chemin, taille, "archivage")
                except Exception as e:
                    erreurs.append(str(e))
            if erreurs:
                messagebox.showerror("Erreurs", "\n".join(erreurs))
            else:
                messagebox.showinfo("Archivage réussi", f"{nb} fichier(s) archivé(s) avec succès.")
            win.destroy()

        def exporter_csv():
            if not cases:
                return
            chemin = filedialog.asksaveasfilename(
                title="Enregistrer le rapport",
                defaultextension=".csv",
                initialfile=f"rapport_anciens_{datetime.now().strftime('%Y-%m-%d')}.csv",
                filetypes=[("Fichier CSV", "*.csv")]
            )
            if not chemin:
                return
            with open(chemin, "w", newline="", encoding="utf-8-sig") as f:
                w = csv.writer(f, delimiter=";")
                w.writerow(["Nom", "Chemin complet", "Taille", "Dernière modification"])
                for _, fp, taille in cases:
                    try:
                        mtime = datetime.fromtimestamp(os.path.getmtime(fp)).strftime("%Y-%m-%d")
                    except OSError:
                        mtime = "?"
                    w.writerow([os.path.basename(fp), fp, taille_lisible(taille), mtime])
            messagebox.showinfo("Export réussi", f"Rapport enregistré :\n{chemin}")

        def lancer_scan():
            for w in scroll.winfo_children():
                w.destroy()
            cases.clear()
            lbl_st.configure(text="Recherche en cours…")
            threading.Thread(target=scanner, daemon=True).start()

        def scanner():
            jours = SEUILS.get(seuil_var.get(), 365)
            limite = datetime.now() - timedelta(days=jours)
            trouves = []
            try:
                for racine in self.dossiers_choisis:
                    for root, _, files in os.walk(racine):
                        for fname in files:
                            fp = os.path.join(root, fname)
                            try:
                                mtime = datetime.fromtimestamp(os.path.getmtime(fp))
                                if mtime < limite:
                                    trouves.append((fp, os.path.getsize(fp), mtime))
                            except OSError:
                                pass
            except Exception:
                pass
            trouves.sort(key=lambda x: x[2])
            win.after(0, lambda: afficher(trouves))

        def afficher(trouves):
            if not trouves:
                lbl_st.configure(text=f"✅  Aucun fichier de plus de {seuil_var.get()}.")
                return
            lbl_st.configure(text=f"{len(trouves)} fichier(s) trouvé(s)")
            for fp, taille, mtime in trouves:
                row = ctk.CTkFrame(scroll, fg_color=GRIS_CARD, corner_radius=8,
                                   border_width=1, border_color=GRIS_BORDURE)
                row.pack(fill="x", pady=(0, 4))
                inner = ctk.CTkFrame(row, fg_color="transparent")
                inner.pack(fill="x", padx=12, pady=8)
                var = ctk.BooleanVar(value=False)
                ctk.CTkCheckBox(
                    inner, text="", variable=var,
                    fg_color=VERT, hover_color=VERT_HOVER,
                    checkmark_color="white", width=20
                ).pack(side="left")
                ctk.CTkLabel(
                    inner, text=os.path.basename(fp),
                    font=ctk.CTkFont(family="Segoe UI", size=13, weight="bold"),
                    text_color=TEXTE_PRINCIPAL
                ).pack(side="left", padx=(8, 4))
                parent = os.path.dirname(fp)
                affichage = parent if len(parent) < 55 else "..." + parent[-52:]
                lbl_p = ctk.CTkLabel(
                    inner, text=affichage,
                    font=ctk.CTkFont(family="Segoe UI", size=11),
                    text_color=TEXTE_SECONDAIRE
                )
                lbl_p.pack(side="left", padx=(0, 8))
                if len(parent) >= 55:
                    Tooltip(lbl_p, parent)
                ctk.CTkLabel(
                    inner, text=mtime.strftime("%d/%m/%Y"),
                    font=ctk.CTkFont(family="Segoe UI", size=11),
                    text_color=ORANGE
                ).pack(side="right")
                ctk.CTkLabel(
                    inner, text=taille_lisible(taille),
                    font=ctk.CTkFont(family="Segoe UI", size=11),
                    text_color=TEXTE_SECONDAIRE
                ).pack(side="right", padx=(0, 10))
                cases.append((var, fp, taille))

        ctk.CTkButton(
            btn_frame, text="🔍  Rechercher",
            fg_color=BLEU, hover_color=BLEU_HOVER,
            corner_radius=6, height=34, width=130,
            command=lancer_scan
        ).pack(side="left", padx=(0, 8))
        ctk.CTkButton(
            btn_frame, text="Tout cocher",
            fg_color="#F1F5F9", hover_color="#E2E8F0",
            text_color=TEXTE_PRINCIPAL, corner_radius=6, height=34, width=110,
            command=lambda: [var.set(True) for var, _, _ in cases]
        ).pack(side="left", padx=(0, 8))
        ctk.CTkButton(
            btn_frame, text="📦  Archiver",
            fg_color=VERT, hover_color=VERT_HOVER,
            corner_radius=6, height=34,
            command=archiver
        ).pack(side="left", padx=(0, 8))
        ctk.CTkButton(
            btn_frame, text="📄  Exporter CSV",
            fg_color="#F1F5F9", hover_color="#E2E8F0",
            text_color=TEXTE_PRINCIPAL, corner_radius=6, height=34,
            command=exporter_csv
        ).pack(side="left")

        lancer_scan()

    # ── Fichiers sensibles ─────────────────────────────────────────────────────

    def _ouvrir_fichiers_sensibles(self):
        if not self._verifier_dossiers():
            return
        win = ctk.CTkToplevel(self)
        win.title("Fichiers sensibles")
        win.geometry("760x540")
        win.configure(fg_color=GRIS_FOND)
        win.grab_set()
        ctk.CTkLabel(
            win, text="🔒  Fichiers au nom sensible",
            font=ctk.CTkFont(family="Segoe UI", size=16, weight="bold"),
            text_color=TEXTE_PRINCIPAL
        ).pack(anchor="w", padx=24, pady=(20, 4))
        ctk.CTkLabel(
            win,
            text="Ces fichiers contiennent des mots-clés sensibles dans leur nom. Vérifiez qu'ils sont bien protégés.",
            font=ctk.CTkFont(family="Segoe UI", size=12),
            text_color=TEXTE_SECONDAIRE
        ).pack(anchor="w", padx=24, pady=(0, 8))
        lbl_st = ctk.CTkLabel(
            win, text="Recherche en cours…",
            font=ctk.CTkFont(family="Segoe UI", size=12),
            text_color=TEXTE_SECONDAIRE
        )
        lbl_st.pack(anchor="w", padx=24)
        scroll = ctk.CTkScrollableFrame(win, fg_color="transparent")
        scroll.pack(fill="both", expand=True, padx=24, pady=(8, 20))

        def scanner():
            trouves = []
            try:
                for racine in self.dossiers_choisis:
                    for root, _, files in os.walk(racine):
                        for fname in files:
                            if any(mot in fname.lower() for mot in self.MOTS_SENSIBLES):
                                fp = os.path.join(root, fname)
                                try:
                                    taille = os.path.getsize(fp)
                                except OSError:
                                    taille = 0
                                trouves.append((fp, taille))
            except Exception:
                pass
            win.after(0, lambda: afficher(trouves))

        def afficher(trouves):
            if not trouves:
                lbl_st.configure(text="✅  Aucun fichier sensible trouvé.")
                return
            lbl_st.configure(
                text=f"⚠️  {len(trouves)} fichier(s) sensible(s) — vérifiez leur emplacement"
            )
            for fp, taille in trouves:
                row = ctk.CTkFrame(scroll, fg_color=GRIS_CARD, corner_radius=8,
                                   border_width=1, border_color="#FCD34D")
                row.pack(fill="x", pady=(0, 4))
                inner = ctk.CTkFrame(row, fg_color="transparent")
                inner.pack(fill="x", padx=12, pady=10)
                ctk.CTkLabel(
                    inner, text="⚠️",
                    font=ctk.CTkFont(family="Segoe UI", size=16)
                ).pack(side="left", padx=(0, 8))
                ctk.CTkLabel(
                    inner, text=os.path.basename(fp),
                    font=ctk.CTkFont(family="Segoe UI", size=13, weight="bold"),
                    text_color=TEXTE_PRINCIPAL
                ).pack(side="left", padx=(0, 4))
                parent = os.path.dirname(fp)
                affichage = parent if len(parent) < 55 else "..." + parent[-52:]
                lbl_p = ctk.CTkLabel(
                    inner, text=affichage,
                    font=ctk.CTkFont(family="Segoe UI", size=11),
                    text_color=TEXTE_SECONDAIRE
                )
                lbl_p.pack(side="left")
                if len(parent) >= 55:
                    Tooltip(lbl_p, parent)
                ctk.CTkLabel(
                    inner, text=taille_lisible(taille),
                    font=ctk.CTkFont(family="Segoe UI", size=11),
                    text_color=TEXTE_SECONDAIRE
                ).pack(side="right")

        threading.Thread(target=scanner, daemon=True).start()


if __name__ == "__main__":
    app = DoublonFinder()
    app.mainloop()
