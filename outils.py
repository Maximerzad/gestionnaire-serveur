"""Outils : espace disque, fichiers inutiles, anciens, sensibles.
Tous réutilisent le recensement de l'analyse (une seule passe de scan)."""

import os
import csv
import shutil
import fnmatch
import threading
from datetime import datetime, timedelta
from tkinter import filedialog, messagebox

import customtkinter as ctk

import moteur as m

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

PATTERNS_INUTILES = [
    "Thumbs.db", "desktop.ini", ".DS_Store", "ehthumbs.db",
    "*.tmp", "*.log", "*.bak", "~$*",
]
MOTS_SENSIBLES = [
    "mot de passe", "motdepasse", "mdp", "password", "passwd",
    "identifiant", "login", "secret", "credential", " pin", "_pin",
]


def _avec_scan(app, lbl_st, rappel):
    """Récupère le recensement (réutilisé si dispo) dans un thread,
    puis appelle `rappel(fichiers)` sur le thread UI."""
    def travail():
        def maj(nb):
            app._sur_ui(lambda n=nb: lbl_st.configure(
                text=f"Parcours du serveur…  {n:,} fichiers recensés"))
        fichiers = app.obtenir_scan_partage(maj)
        app._sur_ui(lambda: rappel(fichiers))
    threading.Thread(target=travail, daemon=True).start()


def _fenetre(app, titre, geometrie):
    win = ctk.CTkToplevel(app)
    win.title(titre)
    win.geometry(geometrie)
    win.configure(fg_color=GRIS_FOND)
    win.grab_set()
    return win


def _ligne_fichier(scroll, chemin, colonnes_droite, case_var=None,
                   couleur_case=ROUGE, bordure=GRIS_BORDURE):
    from interface import Tooltip
    row = ctk.CTkFrame(scroll, fg_color=GRIS_CARD, corner_radius=8,
                       border_width=1, border_color=bordure)
    row.pack(fill="x", pady=(0, 4))
    inner = ctk.CTkFrame(row, fg_color="transparent")
    inner.pack(fill="x", padx=12, pady=8)
    if case_var is not None:
        ctk.CTkCheckBox(
            inner, text="", variable=case_var,
            fg_color=couleur_case,
            hover_color=ROUGE_HOVER if couleur_case == ROUGE else VERT_HOVER,
            checkmark_color="white", width=20
        ).pack(side="left")
    ctk.CTkLabel(
        inner, text=os.path.basename(chemin),
        font=ctk.CTkFont(family="Segoe UI", size=13, weight="bold"),
        text_color=TEXTE_PRINCIPAL
    ).pack(side="left", padx=(8, 4))
    parent = os.path.dirname(chemin)
    affichage = parent if len(parent) < 55 else "..." + parent[-52:]
    lbl_p = ctk.CTkLabel(
        inner, text=affichage,
        font=ctk.CTkFont(family="Segoe UI", size=11),
        text_color=TEXTE_SECONDAIRE
    )
    lbl_p.pack(side="left", padx=(0, 8))
    if len(parent) >= 55:
        Tooltip(lbl_p, parent)
    for texte, couleur in reversed(colonnes_droite):
        ctk.CTkLabel(
            inner, text=texte,
            font=ctk.CTkFont(family="Segoe UI", size=11),
            text_color=couleur
        ).pack(side="right", padx=(10, 0))


# ── Espace disque ────────────────────────────────────────────────────────────

def ouvrir_espace_disque(app):
    if not app._verifier_dossiers():
        return
    win = _fenetre(app, "Espace disque", "680x520")
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

    racines = [os.path.normpath(r) for r in app.dossiers_choisis]

    def afficher(fichiers):
        # Agrégation par sous-dossier direct des racines — aucun re-parcours
        tailles = {}
        for fp, taille, _ in fichiers:
            fpn = os.path.normpath(fp)
            for racine in racines:
                if fpn.startswith(racine + os.sep):
                    reste = fpn[len(racine) + 1:]
                    premier = reste.split(os.sep, 1)
                    if len(premier) == 2:
                        cle = os.path.join(racine, premier[0])
                        tailles[cle] = tailles.get(cle, 0) + taille
                    break
        top = sorted(tailles.items(), key=lambda x: x[1], reverse=True)[:10]
        if not top:
            lbl_st.configure(text="Aucun sous-dossier trouvé.")
            return
        total_general = sum(t for _, t in top)
        lbl_st.configure(text=f"Top {len(top)}  ·  {m.taille_lisible(total_general)} au total")
        taille_max = top[0][1] or 1
        medailles = {1: "🥇", 2: "🥈", 3: "🥉"}
        for rang, (chemin, taille) in enumerate(top, 1):
            card = ctk.CTkFrame(scroll, fg_color=GRIS_CARD, corner_radius=8,
                                border_width=1, border_color=GRIS_BORDURE)
            card.pack(fill="x", pady=(0, 6))
            inner = ctk.CTkFrame(card, fg_color="transparent")
            inner.pack(fill="x", padx=14, pady=10)
            prefixe = medailles.get(rang, f"{rang}.")
            nom_affiche = chemin if len(chemin) < 58 else "..." + chemin[-55:]
            ligne = ctk.CTkFrame(inner, fg_color="transparent")
            ligne.pack(fill="x")
            ctk.CTkLabel(
                ligne, text=f"{prefixe}  📁  {nom_affiche}",
                font=ctk.CTkFont(family="Segoe UI", size=13, weight="bold"),
                text_color=TEXTE_PRINCIPAL
            ).pack(side="left")
            ctk.CTkLabel(
                ligne, text=m.taille_lisible(taille),
                font=ctk.CTkFont(family="Segoe UI", size=13, weight="bold"),
                text_color=BLEU
            ).pack(side="right")
            bar = ctk.CTkProgressBar(
                inner, fg_color="#E2E8F0", progress_color=BLEU,
                height=8, corner_radius=3
            )
            bar.pack(fill="x", pady=(6, 0))
            bar.set(taille / taille_max)

    _avec_scan(app, lbl_st, afficher)


# ── Fichiers inutiles ────────────────────────────────────────────────────────

def ouvrir_fichiers_inutiles(app):
    if not app._verifier_dossiers():
        return
    win = _fenetre(app, "Fichiers inutiles", "760x560")
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
        verbe = "Envoyer à la corbeille" if m.send2trash else "Supprimer définitivement"
        if not messagebox.askyesno("Confirmer", f"{verbe} {nb} fichier(s) ?"):
            return

        def travail():
            erreurs = []
            faits = set()
            for c in sel:
                try:
                    m.supprimer_element(c, "fichier")
                    m.logger_suppression(c, 0, "fichier inutile")
                    faits.add(c)
                except Exception as e:
                    erreurs.append(str(e))
            if app._scan_partage is not None:
                app._scan_partage = [f for f in app._scan_partage if f[0] not in faits]

            def fin():
                if erreurs:
                    messagebox.showerror("Erreurs", "\n".join(erreurs[:15]))
                else:
                    messagebox.showinfo("Suppression réussie", f"{nb} fichier(s) supprimé(s).")
                win.destroy()
            app._sur_ui(fin)
        threading.Thread(target=travail, daemon=True).start()

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

    def afficher(fichiers):
        trouves = [(fp, taille) for fp, taille, _ in fichiers
                   if any(fnmatch.fnmatch(os.path.basename(fp), p)
                          for p in PATTERNS_INUTILES)]
        if not trouves:
            lbl_st.configure(text="✅  Aucun fichier inutile trouvé.")
            return
        total = sum(t for _, t in trouves)
        texte = f"{len(trouves)} fichier(s) inutile(s)  ·  {m.taille_lisible(total)} récupérables"
        if len(trouves) > 800:
            texte += "  ·  affichage limité aux 800 premiers"
        lbl_st.configure(text=texte)
        for chemin, taille in trouves[:800]:
            var = ctk.BooleanVar(value=False)
            _ligne_fichier(scroll, chemin,
                           [(m.taille_lisible(taille), TEXTE_SECONDAIRE)],
                           case_var=var)
            cases.append((var, chemin))

    _avec_scan(app, lbl_st, afficher)


# ── Fichiers anciens ─────────────────────────────────────────────────────────

def ouvrir_fichiers_anciens(app):
    if not app._verifier_dossiers():
        return
    win = _fenetre(app, "Fichiers anciens", "820x600")
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
    lbl_st = ctk.CTkLabel(
        inner_o, text="",
        font=ctk.CTkFont(family="Segoe UI", size=12),
        text_color=TEXTE_SECONDAIRE
    )

    scroll = ctk.CTkScrollableFrame(win, fg_color="transparent")
    scroll.pack(fill="both", expand=True, padx=24)
    btn_frame = ctk.CTkFrame(win, fg_color="transparent")
    btn_frame.pack(fill="x", padx=24, pady=12)

    cases = []
    tous_trouves = []
    fichiers_scannes = []
    SEUILS = {"6 mois": 183, "1 an": 365, "2 ans": 730, "3 ans": 1095,
              "5 ans": 1825, "10 ans": 3650, "15 ans": 5475, "20 ans": 7300}

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

        def travail():
            erreurs = []
            for chemin, taille in sel:
                try:
                    dossier_arch = os.path.join(os.path.dirname(chemin), nom_arch)
                    os.makedirs(dossier_arch, exist_ok=True)
                    dest = os.path.join(dossier_arch, os.path.basename(chemin))
                    if os.path.exists(dest):
                        base, ext = os.path.splitext(os.path.basename(chemin))
                        dest = os.path.join(
                            dossier_arch,
                            f"{base}_{int(datetime.now().timestamp())}{ext}")
                    shutil.move(chemin, dest)
                    m.logger_suppression(chemin, taille, "archivage")
                except Exception as e:
                    erreurs.append(str(e))
            app.invalider_scan_partage()

            def fin():
                if erreurs:
                    messagebox.showerror("Erreurs", "\n".join(erreurs[:15]))
                else:
                    messagebox.showinfo("Archivage réussi",
                                        f"{nb} fichier(s) archivé(s) avec succès.")
                win.destroy()
            app._sur_ui(fin)
        threading.Thread(target=travail, daemon=True).start()

    def exporter_csv():
        if not tous_trouves:
            messagebox.showinfo("Rien à exporter", "Lancez d'abord une recherche.")
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
            for fp, taille, mtime in tous_trouves:
                w.writerow([os.path.basename(fp), fp, m.taille_lisible(taille),
                            mtime.strftime("%Y-%m-%d")])
        messagebox.showinfo("Export réussi",
                            f"{len(tous_trouves)} fichier(s) exporté(s) :\n{chemin}")

    def filtrer():
        """Re-filtre instantanément le recensement déjà en mémoire."""
        for w in scroll.winfo_children():
            w.destroy()
        cases.clear()
        tous_trouves.clear()
        jours = SEUILS.get(seuil_var.get(), 365)
        limite = (datetime.now() - timedelta(days=jours)).timestamp()
        trouves = [(fp, taille, datetime.fromtimestamp(mtime))
                   for fp, taille, mtime in fichiers_scannes if mtime < limite]
        trouves.sort(key=lambda x: x[2])
        if not trouves:
            lbl_st.configure(text=f"✅  Aucun fichier de plus de {seuil_var.get()}.")
            return
        tous_trouves.extend(trouves)
        total = sum(t for _, t, _ in trouves)
        texte = f"{len(trouves)} fichier(s)  ·  {m.taille_lisible(total)}"
        if len(trouves) > 800:
            texte += "  ·  affichage des 800 plus anciens (CSV = liste complète)"
        lbl_st.configure(text=texte)
        for fp, taille, mtime in trouves[:800]:
            var = ctk.BooleanVar(value=False)
            _ligne_fichier(scroll, fp,
                           [(m.taille_lisible(taille), TEXTE_SECONDAIRE),
                            (mtime.strftime("%d/%m/%Y"), ORANGE)],
                           case_var=var, couleur_case=VERT)
            cases.append((var, fp, taille))

    def apres_scan(fichiers):
        fichiers_scannes.clear()
        fichiers_scannes.extend(fichiers)
        filtrer()

    ctk.CTkOptionMenu(
        inner_o, values=list(SEUILS.keys()),
        variable=seuil_var,
        command=lambda _v: filtrer(),
        fg_color=BLEU, button_color=BLEU_HOVER,
        dropdown_fg_color=GRIS_CARD, text_color="white",
        font=ctk.CTkFont(family="Segoe UI", size=13),
        width=120, height=34
    ).pack(side="left", padx=(0, 16))
    lbl_st.pack(side="left")

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

    lbl_st.configure(text="Recherche en cours…")
    _avec_scan(app, lbl_st, apres_scan)


# ── Fichiers sensibles ───────────────────────────────────────────────────────

def ouvrir_fichiers_sensibles(app):
    if not app._verifier_dossiers():
        return
    win = _fenetre(app, "Fichiers sensibles", "760x540")
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

    def afficher(fichiers):
        trouves = [(fp, taille) for fp, taille, _ in fichiers
                   if any(mot in os.path.basename(fp).lower()
                          for mot in MOTS_SENSIBLES)]
        if not trouves:
            lbl_st.configure(text="✅  Aucun fichier sensible trouvé.")
            return
        lbl_st.configure(
            text=f"⚠️  {len(trouves)} fichier(s) sensible(s) — vérifiez leur emplacement")
        for fp, taille in trouves[:500]:
            _ligne_fichier(scroll, fp,
                           [(m.taille_lisible(taille), TEXTE_SECONDAIRE)],
                           bordure="#FCD34D")

    _avec_scan(app, lbl_st, afficher)
