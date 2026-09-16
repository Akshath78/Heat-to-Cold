from __future__ import annotations
import csv
import json
import math
import os
from pathlib import Path

from . import *
from .optimizer import DESIGN_VARIABLES

try:
    import numpy as np
    HAVE_NUMPY = True
except ImportError:
    np = None
    HAVE_NUMPY = False

def generate_sih_visuals(rows: list[dict], output_dir: str = ".") -> dict:
    """Generate presentation-ready PV-vs-battery visuals from diagnostic rows.

    Creates separate figures (no subplots):
      1) pv_battery_unmet_heatmap.png
      2) pv_battery_rh_heatmap.png
      3) pv_battery_feasible_frontier.png
      4) pv_battery_sih_dashboard.png (single-panel annotated matrix)

    Matplotlib is imported lazily so the core simulation remains usable without it.
    """
    os.makedirs(output_dir, exist_ok=True)
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import numpy as np
        from matplotlib.colors import LogNorm, Normalize
    except Exception as exc:
        print(f"[PLOTS] matplotlib unavailable; visuals skipped: {exc}", flush=True)
        return {}

    clean = []
    for r in rows:
        try:
            pv = float(r["pv_kWp"])
            batt = float(r["battery_kWh"])
            unmet = float(r["unmet_kWh"])
            rh = float(r["rh_high_hours"])
            feasible = str(r["feasible"]).lower() == "true" or r["feasible"] is True
        except (KeyError, TypeError, ValueError):
            continue
        if all(math.isfinite(v) for v in (pv, batt, unmet, rh)):
            clean.append((pv, batt, unmet, rh, feasible))
    if not clean:
        print("[PLOTS] no finite PV/battery diagnostic rows available; visuals skipped", flush=True)
        return {}

    pvs = sorted({x[0] for x in clean})
    batts = sorted({x[1] for x in clean})
    unmet_map = {(pv, b): u for pv, b, u, _, _ in clean}
    rh_map = {(pv, b): h for pv, b, _, h, _ in clean}
    feas_map = {(pv, b): f for pv, b, _, _, f in clean}

    P, B = np.meshgrid(pvs, batts)
    U = np.array([[unmet_map.get((pv, b), np.nan) for pv in pvs] for b in batts], dtype=float)
    H = np.array([[rh_map.get((pv, b), np.nan) for pv in pvs] for b in batts], dtype=float)

    paths = {}

    # 1. Unmet-refrigeration heatmap — the strongest SIH visual.
    fig, ax = plt.subplots(figsize=(10, 6))
    positive = U[U > 1e-6]
    vmax = float(np.nanmax(U))
    if positive.size and vmax > 1e-6:
        vmin = max(float(np.nanmin(positive)), 1e-3)
        mesh = ax.pcolormesh(P, B, np.where(U > 1e-6, U, 1e-6), shading="auto", norm=LogNorm(vmin=vmin, vmax=max(vmax, vmin)))
    else:
        mesh = ax.pcolormesh(P, B, U, shading="auto", norm=Normalize(vmin=0.0, vmax=max(vmax, 1.0)))
    cbar = fig.colorbar(mesh, ax=ax)
    cbar.set_label("Annual unmet refrigeration (kWh)")
    for pv, b, u, rh, feasible in clean:
        if feasible:
            ax.scatter(pv, b, marker="*", s=170, facecolors="white", edgecolors="black", linewidths=1.2, zorder=5)
            ax.text(pv, b, "  FEASIBLE", fontsize=8, va="center", ha="left", weight="bold")
        else:
            ax.scatter(pv, b, marker="x", s=45, color="black", linewidths=1.2, zorder=4)
    ax.set_xlabel("PV capacity (kWp)")
    ax.set_ylabel("Battery capacity (kWh)")
    ax.set_title("PV vs Battery: Refrigeration Service Feasibility")
    ax.grid(True, alpha=0.2)
    fig.tight_layout()
    out = os.path.join(output_dir, "pv_battery_unmet_heatmap.png")
    fig.savefig(out, dpi=220, bbox_inches="tight")
    plt.close(fig)
    paths["unmet_heatmap"] = out

    # 2. RH violation heatmap.
    fig, ax = plt.subplots(figsize=(10, 6))
    mesh = ax.pcolormesh(P, B, H, shading="auto", vmin=0.0, vmax=max(float(np.nanmax(H)), 1.0))
    cbar = fig.colorbar(mesh, ax=ax)
    cbar.set_label("Hours above RH upper limit")
    for pv, b, u, rh, feasible in clean:
        if feasible:
            ax.scatter(pv, b, marker="*", s=170, facecolors="white", edgecolors="black", linewidths=1.2, zorder=5)
        else:
            ax.scatter(pv, b, marker="x", s=45, color="black", linewidths=1.2, zorder=4)
    ax.set_xlabel("PV capacity (kWp)")
    ax.set_ylabel("Battery capacity (kWh)")
    ax.set_title("Humidity Performance Across PV–Battery Designs")
    ax.grid(True, alpha=0.2)
    fig.tight_layout()
    out = os.path.join(output_dir, "pv_battery_rh_heatmap.png")
    fig.savefig(out, dpi=220, bbox_inches="tight")
    plt.close(fig)
    paths["rh_heatmap"] = out

    # 3. Minimum feasible battery frontier by PV.
    fig, ax = plt.subplots(figsize=(10, 6))
    for pv in pvs:
        feasible_b = sorted(b for p, b, _, _, f in clean if p == pv and f)
        if feasible_b:
            ax.scatter(pv, feasible_b[0], s=130, marker="*", edgecolors="black", facecolors="white", linewidths=1.2, zorder=4)
            ax.text(pv, feasible_b[0], f"  {feasible_b[0]:.0f} kWh", fontsize=9, va="center")
        else:
            tested_b = sorted(b for p, b, _, _, _ in clean if p == pv)
            if tested_b:
                ax.scatter(pv, max(tested_b), s=70, marker="x", color="black")
                ax.text(pv, max(tested_b), "  no feasible point", fontsize=8, va="center")
    ax.set_xlabel("PV capacity (kWp)")
    ax.set_ylabel("Minimum tested feasible battery (kWh)")
    ax.set_title("Minimum Battery Requirement vs PV Capacity")
    ax.grid(True, alpha=0.25)
    fig.tight_layout()
    out = os.path.join(output_dir, "pv_battery_feasible_frontier.png")
    fig.savefig(out, dpi=220, bbox_inches="tight")
    plt.close(fig)
    paths["frontier"] = out

    # 4. Single-panel SIH matrix with the headline result.
    fig, ax = plt.subplots(figsize=(12, 7))
    ax.axis("off")
    feasible = [x for x in clean if x[4]]
    best = min(clean, key=lambda x: (0 if x[4] else 1, x[1], x[0]))
    if feasible:
        headline = f"FEASIBLE REGION FOUND  |  {len(feasible)} / {len(clean)} tested designs"
        bpv, bb, bu, brh, _ = min(feasible, key=lambda x: (x[1], x[0]))
        subtitle = f"Smallest tested feasible battery: {bb:.0f} kWh at {bpv:.0f} kWp PV"
    else:
        headline = "NO FEASIBLE POINT IN CURRENT PV–BATTERY SCREEN"
        bpv, bb, bu, brh, _ = best
        subtitle = f"Best tested point: {bpv:.0f} kWp PV + {bb:.0f} kWh battery"

    matrix = [["PV (kWp)", *[f"{b:.0f} kWh" for b in batts]]]
    for pv in pvs:
        row = [f"{pv:.0f}"]
        for b in batts:
            item = next((x for x in clean if x[0] == pv and x[1] == b), None)
            if item is None:
                row.append("—")
            else:
                _, _, u, rh, f = item
                row.append("✓\n0 kWh unmet" if f else f"{u:.1f} kWh unmet\n{rh:.0f} h RH")
        matrix.append(row)
    table = ax.table(cellText=matrix[1:], colLabels=matrix[0], cellLoc="center", loc="center", bbox=[0.02, 0.15, 0.96, 0.70])
    table.auto_set_font_size(False)
    table.set_fontsize(10)
    table.scale(1, 1.6)
    for (r, c), cell in table.get_celld().items():
        cell.set_linewidth(0.8)
        if r == 0 or c == 0:
            cell.set_text_props(weight="bold")
    ax.text(0.5, 0.95, headline, transform=ax.transAxes, ha="center", va="center", fontsize=17, weight="bold")
    ax.text(0.5, 0.89, subtitle, transform=ax.transAxes, ha="center", va="center", fontsize=12)
    ax.text(0.5, 0.07, "Goal: 0 kWh unmet refrigeration + 0 h RH violation + all thermal constraints satisfied", transform=ax.transAxes, ha="center", va="center", fontsize=10)
    fig.tight_layout()
    out = os.path.join(output_dir, "pv_battery_sih_visual.png")
    fig.savefig(out, dpi=220, bbox_inches="tight")
    plt.close(fig)
    paths["sih_visual"] = out

    print("[PLOTS] generated presentation visuals:", flush=True)
    for key, path in paths.items():
        print(f"        {key}: {path}", flush=True)
    return paths


def _rankdata_simple(a):
    """Tie-aware average ranks using only NumPy; Spearman helper."""
    a=np.asarray(a,float)
    order=np.argsort(a,kind="mergesort")
    ranks=np.empty(len(a),float)
    i=0
    while i<len(a):
        j=i+1
        while j<len(a) and a[order[j]]==a[order[i]]:
            j+=1
        ranks[order[i:j]]=(i+j-1)/2.0+1.0
        i=j
    return ranks


def _safe_spearman(x,y):
    rx=_rankdata_simple(x); ry=_rankdata_simple(y)
    if len(rx)<3 or np.std(rx)<=1e-12 or np.std(ry)<=1e-12:
        return 0.0
    return float(np.corrcoef(rx,ry)[0,1])


def _objective_names():
    return ["PV capacity (kWp)","Battery capacity (kWh)","PCM mass (kg)","Compressor energy (kWh)"]


def generate_optimization_visuals(candidates: list[dict], front: list[dict], best: Optional[dict], output_dir: str = ".", history_records=None) -> dict:
    """Research-style optimization figure pack from *actual* NSGA-II evaluations.

    The pack intentionally distinguishes evaluated designs from the final Pareto
    set and never fabricates missing simulation data. It includes classical
    Pareto, 3-D trade-off, convergence, hypervolume, parallel-coordinate,
    sensitivity, constraint, and design-space figures used in optimization
    studies. See examples in recent NSGA-II energy-system literature. 
    """
    os.makedirs(output_dir, exist_ok=True)
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib.backends.backend_pdf import PdfPages
        import numpy as np
    except Exception as exc:
        print(f"[PLOTS] matplotlib unavailable; optimization visuals skipped: {exc}", flush=True)
        return {}

    # Journal-like, presentation-friendly rendering. Colors are intentionally
    # restrained but categorical separation is retained where needed.
    plt.rcParams.update({
        "figure.dpi": 120, "savefig.dpi": 320,
        "font.size": 10, "axes.titlesize": 13, "axes.labelsize": 11,
        "legend.fontsize": 9, "axes.spines.top": False, "axes.spines.right": False,
        "axes.grid": True, "grid.alpha": 0.18,
    })

    clean=[]
    for c in candidates:
        try:
            x=np.asarray(c["x"],float)
            o=np.asarray(c.get("objectives",[]),float)
            if len(x)!=8 or len(o)!=4 or not np.all(np.isfinite(x)) or not np.all(np.isfinite(o)):
                continue
            g=np.asarray(c.get("constraints",[]),float)
            cv=float(np.sum(np.maximum(0.0,g))) if len(g) else 0.0
            clean.append({
                "x":x,"o":o,"feasible":bool(c.get("feasible",False)),"cv":cv,
                "unmet":float(c.get("unmet_kWh",0.0) or 0.0),
                "rh":float(c.get("rh_violation_hours",0.0) or 0.0),
                "reason":str(c.get("failure_reason","") or ""),
            })
        except Exception:
            pass
    if not clean:
        return {}

    # Deduplicate by exact rounded design vector for plotting.
    unique={tuple(np.round(d["x"],8)):d for d in clean}
    clean=list(unique.values())
    feas=[d for d in clean if d["feasible"]]
    par=[]
    for i,d in enumerate(feas):
        if not any(np.all(e["o"]<=d["o"]+1e-12) and np.any(e["o"]<d["o"]-1e-12) for j,e in enumerate(feas) if i!=j):
            par.append(d)
    if not par:
        par=[d for d in clean if d["feasible"]]

    paths={}
    figures=[]
    def savefig(fig,name):
        path=os.path.join(output_dir,name)
        fig.savefig(path,bbox_inches="tight")
        figures.append(fig)
        paths[name]=path
        return path

    names=[v[0] for v in DESIGN_VARIABLES]
    obj_names=_objective_names()
    X=np.vstack([d["x"] for d in clean])
    O=np.vstack([d["o"] for d in clean])
    E=np.vstack([d["x"] for d in par]) if par else np.empty((0,8))
    PO=np.vstack([d["o"] for d in par]) if par else np.empty((0,4))

    # 1. 6-panel objective-space Pareto matrix — dense and standard.
    fig,axs=plt.subplots(2,3,figsize=(13,8))
    pairs=[(0,1),(0,2),(0,3),(1,2),(1,3),(2,3)]
    for ax,(i,j) in zip(axs.ravel(),pairs):
        ax.scatter(O[:,i],O[:,j],s=14,alpha=0.18,linewidths=0,label="All evaluated")
        if len(PO):
            ax.scatter(PO[:,i],PO[:,j],s=32,alpha=0.90,linewidths=0,label="Pareto")
        if best is not None:
            bo=np.asarray(best.get("objectives",[]),float)
            if len(bo)==4: ax.scatter([bo[i]],[bo[j]],marker="D",s=75,linewidths=1,label="Recommended")
        ax.set_xlabel(obj_names[i]); ax.set_ylabel(obj_names[j])
        ax.set_title(f"{obj_names[i]} vs {obj_names[j]}")
    handles,labels=axs[0,0].get_legend_handles_labels()
    if handles: fig.legend(handles,labels,loc="upper center",ncol=3,frameon=False,bbox_to_anchor=(0.5,1.01))
    fig.suptitle("Multi-objective Pareto Projection Matrix",fontsize=16,weight="bold",y=1.045)
    fig.tight_layout(); savefig(fig,"pareto_objective_matrix.png")

    # 2. 3-D Pareto front (PV, battery, compressor energy), color by PCM mass.
    fig=plt.figure(figsize=(10,8)); ax=fig.add_subplot(111,projection="3d")
    ax.scatter(O[:,0],O[:,1],O[:,3],s=18,alpha=0.22,linewidths=0)
    if len(PO):
        sc=ax.scatter(PO[:,0],PO[:,1],PO[:,3],c=PO[:,2],s=46,alpha=0.92,linewidths=0)
        cb=fig.colorbar(sc,ax=ax,pad=0.10); cb.set_label("PCM mass (kg)")
    if best is not None:
        bo=np.asarray(best["objectives"],float); ax.scatter([bo[0]],[bo[1]],[bo[3]],marker="D",s=90)
    ax.set_xlabel("PV (kWp)"); ax.set_ylabel("Battery (kWh)"); ax.set_zlabel("Compressor energy (kWh)")
    ax.set_title("Three-Dimensional Pareto Trade-off",pad=18)
    savefig(fig,"pareto_3d_design_tradeoff.png")

    # 3. Dense PV-battery feasibility map, all evaluated points.
    fig,ax=plt.subplots(figsize=(10,7))
    non=[d for d in clean if not d["feasible"]]
    if non:
        vals=np.array([d["unmet"]+0.25*d["rh"] for d in non])
        sc=ax.scatter([d["x"][0] for d in non],[d["x"][1] for d in non],c=vals,s=24,alpha=0.50)
        cb=fig.colorbar(sc,ax=ax); cb.set_label("Unmet refrigeration + 0.25×RH violation")
    if feas: ax.scatter([d["x"][0] for d in feas],[d["x"][1] for d in feas],marker="o",facecolors="white",edgecolors="black",s=55,label="Feasible")
    if best is not None: ax.scatter([best["x"][0]],[best["x"][1]],marker="D",s=100,label="Recommended")
    ax.set_xlabel("PV capacity (kWp)"); ax.set_ylabel("Battery capacity (kWh)")
    ax.set_title("Evaluated PV–Battery Design Space")
    ax.legend(loc="best")
    savefig(fig,"pv_battery_evaluated_design_space.png")

    # 4. Minimum observed feasible battery vs PV.
    fig,ax=plt.subplots(figsize=(10,6))
    pv_groups={}
    for d in feas: pv_groups.setdefault(round(float(d["x"][0]),3),[]).append(float(d["x"][1]))
    if pv_groups:
        pts=sorted((pv,min(bs)) for pv,bs in pv_groups.items())
        ax.plot([p for p,_ in pts],[b for _,b in pts],marker="o",linewidth=2,label="Minimum observed feasible battery")
        for p,b in pts: ax.annotate(f"{b:.1f}",[p,b],textcoords="offset points",xytext=(4,5),fontsize=8)
    ax.set_xlabel("PV capacity (kWp)"); ax.set_ylabel("Minimum observed feasible battery (kWh)")
    ax.set_title("PV–Battery Feasibility Frontier")
    ax.legend(loc="best")
    savefig(fig,"pv_battery_feasibility_frontier.png")

    # 5. Parallel coordinates of Pareto-optimal decision variables.
    if len(E):
        fig,ax=plt.subplots(figsize=(15,7))
        lo=np.nanmin(E,axis=0); hi=np.nanmax(E,axis=0); span=np.where(hi-lo>1e-12,hi-lo,1.0)
        En=(E-lo)/span
        xx=np.arange(8)
        for row in En: ax.plot(xx,row,alpha=0.35,linewidth=1.0)
        if best is not None:
            bn=(np.asarray(best["x"],float)-lo)/span
            ax.plot(xx,bn,linewidth=3,marker="o",label="Recommended")
        ax.set_xticks(xx,labels=[n.replace("_"," ") for n in names],rotation=25,ha="right")
        ax.set_ylim(0,1); ax.set_ylabel("Normalized design value")
        ax.set_title("Pareto-Optimal Design Variable Trade-offs")
        ax.legend(loc="best")
        savefig(fig,"pareto_parallel_coordinates_design.png")

    # 6. Pareto objective parallel coordinates.
    if len(PO):
        fig,ax=plt.subplots(figsize=(12,6))
        lo=np.nanmin(PO,axis=0); hi=np.nanmax(PO,axis=0); span=np.where(hi-lo>1e-12,hi-lo,1.0)
        On=(PO-lo)/span
        for row in On: ax.plot(np.arange(4),row,alpha=0.50,linewidth=1.4)
        ax.set_xticks(np.arange(4),labels=[s.replace(" (","\n(") for s in obj_names])
        ax.set_ylim(0,1); ax.set_ylabel("Normalized objective value (0 = best observed)")
        ax.set_title("Pareto Objective Trade-offs")
        savefig(fig,"pareto_parallel_coordinates_objectives.png")

    # 7. Compact 4x4 sensitivity heatmap for presentation.
    compact_idx = [3, 4, 5, 6]
    compact_names = ["Comp.\n(kW)", "Evap.\n(kW)", "Cond.\n(kW)", "Airflow\n(10³ m³/h)"]
    compact_metrics = np.column_stack([
        O[:, 3],
        np.array([d["unmet"] for d in clean]),
        np.array([d["rh"] for d in clean]),
        O[:, 2],
    ])
    compact_metric_names = ["Comp. E\n(kWh)", "Unmet\n(kWh)", "RH\n(h)", "PCM\n(kg)"]
    compact_corr = np.zeros((4, 4))
    for ii, src_i in enumerate(compact_idx):
        xsrc = X[:, src_i] / 1000.0 if src_i == 6 else X[:, src_i]
        for jj in range(4):
            compact_corr[ii, jj] = _safe_spearman(xsrc, compact_metrics[:, jj])

    fig,ax=plt.subplots(figsize=(5.6,4.4))
    im=ax.imshow(compact_corr,aspect="auto",vmin=-1,vmax=1)
    cb=fig.colorbar(im,ax=ax,shrink=0.78,pad=0.03)
    cb.set_label("ρ",fontsize=9,labelpad=3)
    ax.set_xticks(np.arange(4),labels=compact_metric_names,fontsize=8)
    ax.set_yticks(np.arange(4),labels=compact_names,fontsize=8)
    ax.tick_params(length=0,pad=2)
    for ii in range(4):
        for jj in range(4):
            ax.text(jj,ii,f"{compact_corr[ii,jj]:+.2f}",ha="center",va="center",fontsize=8,weight="bold")
    ax.set_title("Compact Sensitivity (Spearman ρ)",fontsize=11,pad=7)
    fig.tight_layout()
    savefig(fig,"compact_4x4_sensitivity_heatmap.png")

    # 8. Constraint violation distribution.
    cv=np.array([d["cv"] for d in clean])
    fig,ax=plt.subplots(figsize=(10,6))
    positive=cv[cv>1e-10]
    if len(positive): ax.hist(positive,bins=min(30,max(8,int(np.sqrt(len(positive))))),alpha=0.80)
    ax.axvline(1e-8,linestyle="--",linewidth=1.4,label="Feasibility threshold")
    ax.set_xlabel("Total normalized constraint violation")
    ax.set_ylabel("Number of evaluated designs")
    ax.set_yscale("log" if len(positive) and max(1,len(positive))>20 else "linear")
    ax.set_title("Constraint-Violation Distribution")
    ax.legend(loc="best")
    savefig(fig,"constraint_violation_distribution.png")

    # 9. Failure-mode Pareto summary.
    from collections import Counter
    fail=Counter(d["reason"] for d in clean if not d["feasible"])
    if fail:
        items=fail.most_common(10)
        fig,ax=plt.subplots(figsize=(10,6))
        ax.barh([k[:38] for k,_ in items[::-1]],[v for _,v in items[::-1]])
        ax.set_xlabel("Number of evaluated candidates")
        ax.set_title("Observed Infeasibility / Failure Modes")
        savefig(fig,"failure_mode_distribution.png")

    # 10. Resource-bound utilization on Pareto set.
    if len(E):
        xl=np.asarray([v[1] for v in DESIGN_VARIABLES],float); xu=np.asarray([v[2] for v in DESIGN_VARIABLES],float)
        frac_upper=np.mean(np.isclose(E,xu[None,:],rtol=0,atol=np.maximum(1e-6,1e-4*(xu-xl))),axis=0)*100
        fig,ax=plt.subplots(figsize=(12,6))
        ax.bar(np.arange(8),frac_upper)
        ax.set_xticks(np.arange(8),labels=[s.replace("_"," ") for s in names],rotation=25,ha="right")
        ax.set_ylabel("Pareto solutions at upper bound (%)")
        ax.set_ylim(0,100); ax.set_title("Active Upper Bounds in Pareto Solutions")
        savefig(fig,"pareto_upper_bound_activity.png")

    # 11. Recommended design vs reference/median feasible design.
    if best is not None and feas:
        ref=np.median(np.vstack([d["x"] for d in feas]),axis=0)
        bx=np.asarray(best["x"],float)
        fig,ax=plt.subplots(figsize=(12,6))
        q=np.arange(8); width=0.38
        ax.bar(q-width/2,(bx/(np.maximum(ref,1e-9)))*100,width,label="Recommended / median feasible (%)")
        ax.axhline(100,linestyle="--",linewidth=1.0,label="Median feasible = 100%")
        ax.set_xticks(q,labels=[s.replace("_"," ") for s in names],rotation=25,ha="right")
        ax.set_ylabel("Relative value (%)")
        ax.set_title("Recommended Design Relative to Median Feasible Solution")
        ax.legend(loc="best")
        savefig(fig,"recommended_vs_feasible_median.png")

    # 12. Convergence: feasible count + Pareto size + min constraint violation.
    hist=[]
    for rec in (history_records or []):
        for h in rec.get("history",[]):
            hh=dict(h); hh["seed"]=rec.get("seed",1); hist.append(hh)
    if hist:
        fig,axs=plt.subplots(2,1,figsize=(10,8),sharex=True)
        gens=sorted(set(int(h["generation"]) for h in hist))
        # Across multiple seeds, show median and range where available.
        for key,label in [("n_feasible","Feasible solutions"),("n_pareto","Non-dominated solutions")]:
            med=[]; loq=[]; hiq=[]
            for g in gens:
                v=[h[key] for h in hist if int(h["generation"])==g]
                med.append(np.median(v)); loq.append(np.min(v)); hiq.append(np.max(v))
            axs[0].plot(gens,med,marker="o",label=label)
            axs[0].fill_between(gens,loq,hiq,alpha=0.12)
        axs[0].set_ylabel("Count"); axs[0].set_title("NSGA-II Population Convergence"); axs[0].legend()
        med=[]; loq=[]; hiq=[]
        for g in gens:
            v=[h["cv_min"] for h in hist if int(h["generation"])==g]
            med.append(np.median(v)); loq.append(np.min(v)); hiq.append(np.max(v))
        axs[1].plot(gens,med,marker="o",label="Minimum constraint violation")
        axs[1].fill_between(gens,loq,hiq,alpha=0.12)
        axs[1].set_xlabel("Generation"); axs[1].set_ylabel("Minimum CV"); axs[1].set_yscale("log")
        axs[1].set_title("Constraint Convergence")
        savefig(fig,"nsga2_convergence_history.png")

        hv=[h for h in hist if np.isfinite(h.get("hypervolume_normalized",np.nan))]
        if hv:
            fig,ax=plt.subplots(figsize=(10,6))
            for seed in sorted(set(h["seed"] for h in hv)):
                ss=sorted([h for h in hv if h["seed"]==seed],key=lambda z:z["generation"])
                ax.plot([h["generation"] for h in ss],[h["hypervolume_normalized"] for h in ss],marker="o",label=f"Seed {seed}")
            ax.set_xlabel("Generation"); ax.set_ylabel("Normalized hypervolume")
            ax.set_title("Pareto Hypervolume Convergence")
            ax.legend(loc="best")
            savefig(fig,"hypervolume_convergence.png")

    # 13. Objective ranges across all evaluations vs Pareto set.
    if len(PO):
        fig,ax=plt.subplots(figsize=(11,6))
        all_med=np.median(O,axis=0); pareto_med=np.median(PO,axis=0)
        scale=np.maximum(np.max(O,axis=0),1e-12)
        q=np.arange(4)
        ax.plot(q,all_med/scale*100,marker="o",linewidth=2,label="All evaluated median")
        ax.plot(q,pareto_med/scale*100,marker="s",linewidth=2,label="Pareto median")
        ax.set_xticks(q,labels=[s.replace(" (kWh)","\n(kWh)") for s in obj_names])
        ax.set_ylabel("Normalized objective value (% of all-evaluation max)")
        ax.set_title("Objective Compression from Search to Pareto Set")
        ax.legend(loc="best")
        savefig(fig,"objective_distribution_all_vs_pareto.png")

    # 14. Export all figures into one research-figure PDF as well.
    pdf_path=os.path.join(output_dir,"optimization_research_figure_pack.pdf")
    with PdfPages(pdf_path) as pdf:
        for fig in figures:
            pdf.savefig(fig,bbox_inches="tight")
            plt.close(fig)
    paths["figure_pack_pdf"]=pdf_path

    # Save a machine-readable plotting summary.
    summary={
        "n_unique_evaluations":len(clean),"n_feasible":len(feas),"n_pareto":len(par),
        "recommended": None if best is None else {"x":[float(v) for v in best["x"]],"feasible":bool(best.get("feasible",False))},
        "files":paths,
    }
    with open(os.path.join(output_dir,"optimization_plot_summary.json"),"w") as f: json.dump(summary,f,indent=2,default=str)

    print(f"[PLOTS] research-grade figure pack generated from {len(clean)} unique real evaluations",flush=True)
    for key,path in paths.items(): print(f"        {key}: {os.path.abspath(path)}",flush=True)
    return paths

def write_csv(path: str, rows: list[dict]) -> None:
    if not rows:
        with open(path, "w") as f:
            f.write("")
        return
    keys = list(rows[0].keys())
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=keys)
        writer.writeheader()
        for r in rows:
            writer.writerow(r)


# =====================================================================
# Main entry points
# =====================================================================

