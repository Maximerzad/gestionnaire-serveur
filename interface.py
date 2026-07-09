"""Fenêtre principale : sélection des dossiers, analyse, résultats, suppression."""

import os
import csv
import time
import threading
from queue import Queue, Empty
from datetime import datetime
from tkinter import filedialog, messagebox
import tkinter as tk

import customtkinter as ctk

import moteur as m
import outils

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
    def __init__(self):
        super().__init__()
        self.title("Gestionnaire de serveur")
        self.geometry("980x760")
        self.minsize(860, 660)
        self.configure(fg_color=GRIS_FOND)
        self.resizable(True, True)

        self.dossiers_choisis = []
        self.resultats = []
        self._stop_analyse = False
        self._inaccessibles = 0
        self._debut_analyse = None
        self._scan_partage = None      # dernier recensement (chemin, taille, mtime)
        self._selection = set()        # chemins cochés, tous groupes confondus
        self._infos_chemins = {}       # chemin -> (type, taille)
        self._vars_affichees = {}      # chemin -> BooleanVar des lignes visibles
        self._groupes_tries = []
        self._maj_espace_en_pause = False

        self._build_ui()

        # File threads → interface : tkinter n'est jamais touché hors thread UI
        self._ui_queue = Queue()
        self.after(80, self._pomper_ui)

        threading.Thread(target=self._purger_cache, daemon=True).start()

    def _purger_cache(self):
        try:
            m.HashCache().purger_anciens()
        except Exception:
            pass

    def _sur_ui(self, action):
        self._ui_queue.put(action)

    def _pomper_ui(self):
        try:
            while True:
                action = self._ui_queue.get_nowait()
                try:
                    action()
                except Exception:
                    pass
        except Empty:
            pass
        self.after(80, self._pomper_ui)

    # ── Scan partagé avec les outils ──────────────────────────────────────────

    def obtenir_scan_partage(self, maj=None):
        """À appeler depuis un thread. Réutilise le recensement de la dernière
        analyse s'il existe, sinon parcourt le serveur une fois."""
        if self._scan_partage is not None:
            return self._scan_partage
        bruts, etat, file_q, threads = m.collecter_metadonnees(
            list(self.dossiers_choisis), lambda: False)
        while etat["restants"] > 0:
            time.sleep(0.1)
            if maj:
                maj(len(bruts))
        m.terminer_collecte(file_q, threads)
        self._scan_partage = bruts
        return bruts

    def invalider_scan_partage(self):
        self._scan_partage = None

    # ── Interface principale ───────────────────────────────────────────────────

    def _build_ui(self):
        header = ctk.CTkFrame(self, fg_color="#0F172A", corner_radius=0)
        header.pack(fill="x")
        inner_h = ctk.CTkFrame(header, fg_color="transparent")
        inner_h.pack(fill="x", padx=24, pady=16)
        bloc_titre = ctk.CTkFrame(inner_h, fg_color="transparent")
        bloc_titre.pack(side="left")
        ctk.CTkLabel(
            bloc_titre, text="🗂  Gestionnaire de serveur",
            font=ctk.CTkFont(family="Segoe UI", size=21, weight="bold"),
            text_color="white"
        ).pack(anchor="w")
        ctk.CTkLabel(
            bloc_titre, text="Doublons, nettoyage et organisation — analyse complète du serveur",
            font=ctk.CTkFont(family="Segoe UI", size=12),
            text_color="#94A3B8"
        ).pack(anchor="w", pady=(2, 0))
        ctk.CTkButton(
            inner_h, text="📋  Journal",
            font=ctk.CTkFont(family="Segoe UI", size=12),
            fg_color="#1E293B", hover_color="#334155",
            text_color="white", corner_radius=8, height=34, width=110,
            command=self._ouvrir_journal
        ).pack(side="right", pady=6)

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
        self.invalider_scan_partage()
        self._rafraichir_liste_dossiers()

    def _supprimer_dossier_liste(self, dossier):
        self.dossiers_choisis.remove(dossier)
        self.invalider_scan_partage()
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

            icone = "🌐" if m.est_chemin_reseau(dossier) else "📁"
            if m.est_chemin_reseau(dossier):
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
            progress_color=BLEU, height=10, corner_radius=5
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
        self.btn_supprimer = ctk.CTkButton(
            hdr, text="🗑  Supprimer la sélection",
            font=ctk.CTkFont(family="Segoe UI", size=12, weight="bold"),
            fg_color=ROUGE, hover_color=ROUGE_HOVER,
            corner_radius=6, height=30, width=190,
            command=self._supprimer_selection
        )
        self.btn_supprimer.pack(side="right")
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
            text="Ajoutez d'abord les dossiers à analyser, puis sélectionnez un outil. "
                 "Après une analyse, les outils s'ouvrent instantanément.",
            font=ctk.CTkFont(family="Segoe UI", size=12),
            text_color=TEXTE_SECONDAIRE
        ).pack(anchor="w", pady=(0, 14))

        grid = ctk.CTkFrame(parent, fg_color="transparent")
        grid.pack(fill="x")

        liste_outils = [
            ("📊  Espace disque",     "Top 10 des sous-dossiers les plus lourds",
             lambda: outils.ouvrir_espace_disque(self)),
            ("🧹  Fichiers inutiles", "Thumbs.db, .tmp, .log, desktop.ini…",
             lambda: outils.ouvrir_fichiers_inutiles(self)),
            ("📅  Fichiers anciens",  "Non modifiés depuis 6 mois à 20 ans",
             lambda: outils.ouvrir_fichiers_anciens(self)),
            ("🔒  Fichiers sensibles", "Noms contenant : mot de passe, mdp, pin…",
             lambda: outils.ouvrir_fichiers_sensibles(self)),
        ]
        for col, (titre, desc, cmd) in enumerate(liste_outils):
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

    # ── Analyse ────────────────────────────────────────────────────────────────

    def _lancer_analyse(self):
        if not self._verifier_dossiers():
            return
        if not self.check_fichiers.get() and not self.check_dossiers.get():
            messagebox.showwarning("Attention", "Cochez au moins une option.")
            return
        self._stop_analyse = False
        self._inaccessibles = 0
        self._debut_analyse = time.time()
        self._chrono_global = time.time()
        self.btn_analyser.pack_forget()
        self.btn_arreter.pack(side="right")
        self.progress_bar.pack(fill="x")
        self.progress_bar.set(0)
        self._vider_resultats()
        self.resultats = []
        self._selection = set()
        self._vars_affichees = {}
        self._infos_chemins = {}
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

    def _analyser(self, exts_ignorees, taille_min, detect_noms, seuil_sim):
        cache = None
        try:
            cache = m.HashCache()
            detect_fichiers = self.check_fichiers.get()
            detect_dossiers = self.check_dossiers.get()
            stop = lambda: self._stop_analyse

            # Phase 1 : recensement parallèle
            self._update_status_simple("Phase 1/4 — Parcours du serveur…", 0.01)
            bruts, etat, file_q, threads = m.collecter_metadonnees(
                list(self.dossiers_choisis), stop)
            dernier = time.time()
            while etat["restants"] > 0:
                time.sleep(0.1)
                if time.time() - dernier > 0.15:
                    self._update_status_simple(
                        f"Phase 1/4 — {len(bruts):,} fichiers recensés…", 0.03)
                    dernier = time.time()
            m.terminer_collecte(file_q, threads)
            self._inaccessibles += etat["inaccessibles"]
            self._scan_partage = bruts
            if self._stop_analyse:
                self._sur_ui(self._on_arret)
                return

            tous_fichiers = m.filtrer_fichiers(bruts, exts_ignorees, taille_min)
            self._nb_fichiers_scannes = len(bruts)
            doublons = []

            # Phases 2-3 : doublons exacts
            if detect_fichiers and tous_fichiers:
                groupes, inacc = m.detecter_doublons_exacts(
                    tous_fichiers, cache, self._update_status, stop)
                self._inaccessibles += inacc
                if groupes is None:
                    self._sur_ui(self._on_arret)
                    return
                doublons.extend(groupes)

            # Dossiers identiques — en mémoire
            if detect_dossiers and not self._stop_analyse:
                self._update_status_simple("Comparaison des dossiers (en mémoire)…", 0.96)
                doublons.extend(
                    m.grouper_dossiers_en_memoire(bruts, self.dossiers_choisis))

            # Phase 4 : noms similaires
            if detect_noms and tous_fichiers and not self._stop_analyse:
                self._update_status_simple(
                    f"Phase 4/4 — Recherche de noms similaires (seuil {round(seuil_sim*100)}%)…",
                    0.97)
                chemins_exacts = {c for g in doublons if g.get("mode") != "nom"
                                  for c in g["chemins"]}
                fichiers_pour_noms = [
                    f for f in tous_fichiers if f[0] not in chemins_exacts]
                doublons.extend(
                    m.grouper_par_nom_similaire(fichiers_pour_noms, seuil_sim))

            self.resultats = doublons
            self._sur_ui(self._afficher_resultats)
        except Exception as exc:
            message = str(exc)
            self._sur_ui(lambda msg=message: messagebox.showerror("Erreur", msg))
            self._sur_ui(self._reset_btn)
        finally:
            if cache:
                try:
                    cache.commit()
                except Exception:
                    pass

    def _on_arret(self):
        self.label_status.configure(text="Analyse interrompue.")
        self._reset_btn()

    def _update_status_simple(self, texte, progression):
        def maj(msg=texte, p=progression):
            self.label_status.configure(text=msg)
            self.progress_bar.set(p)
        self._sur_ui(maj)

    def _update_status(self, progression, texte):
        pct = int(progression * 100)
        eta_str = ""
        if self._debut_analyse and progression > 0.02:
            elapsed = time.time() - self._debut_analyse
            restant = elapsed / progression * (1 - progression)
            eta_str = f"  ·  {m.formater_duree(restant)} restantes"
        msg = f"{pct}%{eta_str}  ·  {texte}"
        def maj(texte_maj=msg, p=progression):
            self.label_status.configure(text=texte_maj)
            self.progress_bar.set(p)
        self._sur_ui(maj)

    def _vider_resultats(self):
        for w in self.scroll.winfo_children():
            w.destroy()

    # ── Affichage des résultats ────────────────────────────────────────────────

    def _afficher_resultats(self):
        self.progress_bar.set(1)
        duree = time.time() - getattr(self, "_chrono_global", time.time())
        duree_txt = m.formater_duree(duree).replace("~", "")
        msg = f"Analyse terminée en {duree_txt} ✓"
        if self._inaccessibles:
            msg += f"  —  {self._inaccessibles} fichier(s) inaccessible(s) ignoré(s)"
        self.label_status.configure(text=msg)

        nb_scannes = getattr(self, "_nb_fichiers_scannes", 0)

        if not self.resultats:
            self._vider_resultats()
            self.label_nb.configure(text="")
            nb_dossiers = len(self.dossiers_choisis)
            detail = f" dans {nb_dossiers} dossiers" if nb_dossiers > 1 else ""
            ctk.CTkLabel(
                self.scroll,
                text=f"✅  Aucun doublon trouvé{detail} !",
                font=ctk.CTkFont(family="Segoe UI", size=14),
                text_color=VERT
            ).pack(pady=40)
            self._reset_btn()
            self.after(300, lambda: messagebox.showinfo(
                "Analyse terminée",
                f"🔍  {nb_scannes:,} fichiers analysés en {duree_txt}\n\n"
                f"✅  Aucun doublon trouvé — votre serveur est bien rangé !"
            ))
            return

        exacts, noms, fichiers_en_trop, economie_totale = self._construire_liste()
        self._reset_btn()

        recap = (f"🔍  {nb_scannes:,} fichiers analysés en {duree_txt}\n\n"
                 f"📄  {len(exacts)} groupe{'s' if len(exacts) > 1 else ''} de doublons trouvé{'s' if len(exacts) > 1 else ''}\n"
                 f"🗑  {fichiers_en_trop} fichier{'s' if fichiers_en_trop > 1 else ''} en trop\n"
                 f"💾  {m.taille_lisible(economie_totale)} récupérables")
        if noms:
            recap += f"\n\n⚠️  + {len(noms)} groupe{'s' if len(noms) > 1 else ''} de noms similaires à vérifier"
        if m.send2trash is not None:
            recap += "\n\n🛡  Les suppressions passent par la corbeille (récupérables)."
        self.after(300, lambda r=recap: messagebox.showinfo("Analyse terminée", r))

    def _construire_liste(self):
        """Trie les groupes, reconstruit l'index chemin→infos et affiche la
        première page. Utilisé après analyse et après suppression."""
        self._vider_resultats()
        self._vars_affichees = {}
        self._infos_chemins = {}

        exacts = [g for g in self.resultats if g.get("mode") != "nom"]
        noms = [g for g in self.resultats if g.get("mode") == "nom"]
        exacts.sort(key=lambda g: g["taille"] * (len(g["chemins"]) - 1), reverse=True)
        noms.sort(key=lambda g: g.get("similarite", 0), reverse=True)
        self._groupes_tries = exacts + noms
        self._index_affichage = 0

        for g in self._groupes_tries:
            for c in g["chemins"]:
                self._infos_chemins[c] = (g["type"], g["taille"])

        fichiers_en_trop = sum(len(g["chemins"]) - 1 for g in exacts)
        economie_totale = sum(g["taille"] * (len(g["chemins"]) - 1) for g in exacts)
        detail = (f"{len(exacts)} groupe{'s' if len(exacts) > 1 else ''}"
                  f"  ·  {fichiers_en_trop} fichier{'s' if fichiers_en_trop > 1 else ''} en trop"
                  f"  ·  💾 {m.taille_lisible(economie_totale)} récupérables")
        if noms:
            detail += f"  +  {len(noms)} groupe(s) de noms similaires"
        self.label_nb.configure(text=detail)

        self._afficher_page()
        self._update_espace_recuperable()
        return exacts, noms, fichiers_en_trop, economie_totale

    def _creer_case(self, parent, chemin, texte_couleur):
        var = ctk.BooleanVar(value=chemin in self._selection)

        def sur_changement(*_):
            if var.get():
                self._selection.add(chemin)
            else:
                self._selection.discard(chemin)
            self._update_espace_recuperable()

        var.trace_add("write", sur_changement)
        ctk.CTkCheckBox(
            parent, text="Supprimer", variable=var,
            fg_color=ROUGE, hover_color=ROUGE_HOVER,
            checkmark_color="white", width=18,
            font=ctk.CTkFont(family="Segoe UI", size=11),
            text_color=texte_couleur
        ).pack(anchor="w", pady=(4, 0))
        self._vars_affichees[chemin] = var

    def _afficher_page(self):
        """Affiche les résultats par lots de 40 pour ne pas figer l'interface."""
        debut = self._index_affichage
        fin = min(debut + 40, len(self._groupes_tries))
        for groupe in self._groupes_tries[debut:fin]:
            mode_nom = groupe.get("mode") == "nom"
            nb_chemins = len(groupe["chemins"])

            couleur_bord = "#FCD34D" if mode_nom else GRIS_BORDURE
            couleur_entete = "#FFFBEB" if mode_nom else "#EFF6FF"

            card = ctk.CTkFrame(self.scroll, fg_color=GRIS_CARD, corner_radius=10,
                                border_width=1, border_color=couleur_bord)
            card.pack(fill="x", pady=(0, 10))

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
                economie = m.taille_lisible(groupe["taille"] * (nb_chemins - 1))
                titre = f"{emoji}  {nom_rep}"
                sous = (f"{nb_chemins} copies identiques  ·  {m.taille_lisible(groupe['taille'])} chacune"
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

            ctk.CTkFrame(card, fg_color=couleur_bord, height=1).pack(fill="x", padx=8)

            for j, chemin in enumerate(groupe["chemins"]):
                try:
                    st = os.stat(m.chemin_long(chemin))
                    taille_ind = m.taille_lisible(st.st_size)
                    mtime_str = datetime.fromtimestamp(st.st_mtime).strftime("%d/%m/%Y")
                except OSError:
                    taille_ind = "—"
                    mtime_str = "—"

                is_first = (j == 0)
                bg_row = "#F0FDF4" if is_first else "transparent"

                row = ctk.CTkFrame(card, fg_color=bg_row, corner_radius=6)
                row.pack(fill="x", padx=8, pady=(0, 2))
                inner_r = ctk.CTkFrame(row, fg_color="transparent")
                inner_r.pack(fill="x", padx=12, pady=8)

                col_left = ctk.CTkFrame(inner_r, fg_color="transparent", width=110)
                col_left.pack(side="left", fill="y")
                col_left.pack_propagate(False)

                # Le premier de chaque groupe est toujours protégé,
                # y compris pour les noms similaires
                if is_first:
                    texte_badge = "🔒  Référence" if mode_nom else "✅  Conserver"
                    ctk.CTkLabel(
                        col_left, text=texte_badge,
                        font=ctk.CTkFont(family="Segoe UI", size=11, weight="bold"),
                        text_color=VERT, fg_color="#DCFCE7",
                        corner_radius=6, padx=8, pady=3
                    ).pack(anchor="w")
                else:
                    self._creer_case(col_left, chemin,
                                     TEXTE_SECONDAIRE if mode_nom else ROUGE)

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

        self._index_affichage = fin
        restants = len(self._groupes_tries) - fin
        if restants > 0:
            btn_plus = ctk.CTkButton(
                self.scroll,
                text=f"▼  Afficher la suite ({restants} groupe{'s' if restants > 1 else ''} restant{'s' if restants > 1 else ''})",
                font=ctk.CTkFont(family="Segoe UI", size=12, weight="bold"),
                fg_color="#F1F5F9", hover_color="#E2E8F0",
                text_color=TEXTE_PRINCIPAL, corner_radius=8, height=38
            )
            btn_plus.configure(command=lambda b=btn_plus: (b.destroy(), self._afficher_page()))
            btn_plus.pack(fill="x", pady=(4, 8))
        self._update_espace_recuperable()

    # ── Sélection & suppression ────────────────────────────────────────────────

    def _update_espace_recuperable(self):
        if self._maj_espace_en_pause:
            return
        total = sum(self._infos_chemins[c][1] for c in self._selection
                    if c in self._infos_chemins)
        nb = len(self._selection)
        self.label_espace.configure(
            text=f"✓  {nb} élément{'s' if nb > 1 else ''}  ·  {m.taille_lisible(total)} sélectionnés"
            if nb else ""
        )

    def _tout_cocher(self):
        """Coche tous les doublons de tous les groupes (affichés ou non),
        en gardant toujours le premier de chaque groupe."""
        self._maj_espace_en_pause = True
        for groupe in self._groupes_tries:
            for chemin in groupe["chemins"][1:]:
                self._selection.add(chemin)
                var = self._vars_affichees.get(chemin)
                if var is not None:
                    var.set(True)
        self._maj_espace_en_pause = False
        self._update_espace_recuperable()

    def _supprimer_selection(self):
        selection = [(c, *self._infos_chemins[c]) for c in self._selection
                     if c in self._infos_chemins]
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

        # Un dossier sélectionné ne doit pas emporter un fichier "Conserver"
        # d'un autre groupe : on l'écarte de la sélection
        conserves = {g["chemins"][0] for g in self.resultats}
        dossiers_ok = []
        dossiers_ecartes = []
        for chemin, typ, _ in selection:
            if typ != "dossier":
                continue
            prefixe = chemin + os.sep
            if any(k == chemin or k.startswith(prefixe) for k in conserves):
                dossiers_ecartes.append(chemin)
            else:
                dossiers_ok.append(chemin)
        if dossiers_ecartes:
            selection = [s for s in selection if s[0] not in dossiers_ecartes]
            for d in dossiers_ecartes:
                self._selection.discard(d)
                var = self._vars_affichees.get(d)
                if var is not None:
                    var.set(False)
            messagebox.showwarning(
                "Dossiers écartés",
                f"{len(dossiers_ecartes)} dossier(s) retiré(s) de la sélection : "
                "ils contiennent un fichier marqué «Conserver» d'un autre groupe."
            )

        # Éviter la double suppression : les chemins déjà couverts par un
        # dossier sélectionné sont retirés
        prefixes = tuple(d + os.sep for d in dossiers_ok)
        if prefixes:
            selection = [s for s in selection if not s[0].startswith(prefixes)]

        if not selection:
            return
        nb = len(selection)
        apercu = "\n".join(f"• {os.path.basename(c)}" for c, _, _ in selection[:10])
        if nb > 10:
            apercu += f"\n… et {nb - 10} autre(s)"
        if m.send2trash is not None:
            question = (f"Envoyer {nb} élément{'s' if nb > 1 else ''} à la corbeille ?\n\n{apercu}\n\n"
                        "Ils pourront être restaurés depuis la corbeille.\n"
                        "⚠️  Sur un lecteur réseau sans corbeille, la suppression sera définitive.")
        else:
            question = (f"Supprimer {nb} élément{'s' if nb > 1 else ''} définitivement ?\n\n{apercu}\n\n"
                        "Cette action est irréversible.")
        if not messagebox.askyesno("Confirmer la suppression", question):
            return

        self.btn_supprimer.configure(state="disabled", text="Suppression…")
        self.label_status.configure(text="Suppression en cours…")
        threading.Thread(target=self._supprimer_en_arriere_plan,
                         args=(selection,), daemon=True).start()

    def _supprimer_en_arriere_plan(self, selection):
        supprimes = []
        erreurs = []
        nb_corbeille = 0
        for chemin, typ, taille in selection:
            try:
                if not os.path.exists(chemin):
                    supprimes.append(chemin)
                    continue
                if m.supprimer_element(chemin, typ):
                    nb_corbeille += 1
                m.logger_suppression(chemin, taille, typ)
                supprimes.append(chemin)
            except Exception as e:
                erreurs.append(f"{chemin}: {e}")
        self._sur_ui(lambda: self._apres_suppression(supprimes, erreurs, nb_corbeille))

    def _apres_suppression(self, supprimes, erreurs, nb_corbeille=0):
        self.btn_supprimer.configure(state="normal", text="🗑  Supprimer la sélection")
        faits = set(supprimes)

        # Retirer les éléments supprimés des résultats — aucun rescan
        nouveaux = []
        for g in self.resultats:
            restants = [c for c in g["chemins"] if c not in faits]
            if len(restants) >= 2:
                g["chemins"] = restants
                nouveaux.append(g)
        self.resultats = nouveaux
        self._selection -= faits
        if self._scan_partage is not None:
            prefixes_dossiers = tuple(
                c + os.sep for c in faits) if faits else ()
            self._scan_partage = [
                f for f in self._scan_partage
                if f[0] not in faits and not f[0].startswith(prefixes_dossiers)
            ]

        if self.resultats:
            self._construire_liste()
        else:
            self._vider_resultats()
            self.label_nb.configure(text="")
            ctk.CTkLabel(
                self.scroll,
                text="✅  Tous les doublons ont été traités !",
                font=ctk.CTkFont(family="Segoe UI", size=14),
                text_color=VERT
            ).pack(pady=40)
        self._update_espace_recuperable()

        if erreurs:
            self.label_status.configure(
                text=f"{len(supprimes)} supprimé(s), {len(erreurs)} erreur(s).")
            messagebox.showerror("Erreurs de suppression", "\n".join(erreurs[:15]))
        else:
            nb = len(supprimes)
            if nb_corbeille == nb:
                ou = "envoyés à la corbeille" if nb > 1 else "envoyé à la corbeille"
            elif nb_corbeille:
                ou = f"supprimés ({nb_corbeille} à la corbeille)"
            else:
                ou = "supprimés" if nb > 1 else "supprimé"
            self.label_status.configure(text=f"✓  {nb} élément{'s' if nb > 1 else ''} {ou}.")

    def _est_protege(self, chemin):
        cible = os.path.normpath(chemin)
        for racine in self.dossiers_choisis:
            r = os.path.normpath(racine)
            if cible == r or r.startswith(cible + os.sep):
                return True
        return False

    # ── Export & journal ───────────────────────────────────────────────────────

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
        with open(chemin, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f, delimiter=";")
            w.writerow(["Type", "Nom", "Chemin complet", "Taille", "Statut"])
            for g in self.resultats:
                for i, c in enumerate(g["chemins"]):
                    statut = "Conserver" if i == 0 else (
                        "À supprimer" if c in self._selection else "Doublon")
                    w.writerow([g["type"].capitalize(), os.path.basename(c), c,
                                m.taille_lisible(g["taille"]), statut])
        messagebox.showinfo("Export réussi", f"Rapport enregistré :\n{chemin}")

    def _reset_btn(self):
        self.btn_arreter.pack_forget()
        self.btn_analyser.pack(side="right")
        self.btn_analyser.configure(state="normal", text="▶  Lancer l'analyse")

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
            with open(m.JOURNAL_PATH, "r", encoding="utf-8") as f:
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
