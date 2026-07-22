"""Task 1 result payloads, Coder pagination, and the bounded remote collector."""

from __future__ import annotations

import base64
import os
import zlib
from collections.abc import Callable
from dataclasses import dataclass
from typing import assert_never

import dokploy_wizard.proof.model_sync_state as state
from dokploy_wizard import proof
from dokploy_wizard.proof import (
    REQUIRED_RESULT_KEYS as REQUIRED_RESULT_KEYS,
)
from dokploy_wizard.proof import (
    build_result as build_result,
)
from dokploy_wizard.proof import (
    derive_result_from_attestation as derive_result_from_attestation,
)
from dokploy_wizard.proof import (
    result_bytes_from_attestation as result_bytes_from_attestation,
)
from dokploy_wizard.proof import (
    run_bounded_process as run_bounded_process,
)
from dokploy_wizard.proof import (
    validate_attestation as validate_attestation,
)
from dokploy_wizard.proof import (
    verify_result_bytes as verify_result_bytes,
)
from dokploy_wizard.proof.model_sync_artifacts import (
    CaptureSchemaError,
    JsonValue,
    sha256_bytes,
    unlink_exact_regular_bytes,
    write_or_verify_exact_bytes,
)

__all__ = ("REQUIRED_RESULT_KEYS",)

_PREFLIGHT_ENCODED = (
    "c-rkeX_MPVcHie$Ao$<~Z4Noo+D?iwS0!6Ytjb2~@~Bct&9YD!pgF_}1h{BmG_x%J_q?ltZX7&hJCzSsm4;yV>v#3TJNn>Ny4@9J"
    "<^4?;`P3`))Z1lIwJyr-)YF@~N;CMZ?{{SpD-FH+SzoQoV(ZmqmZkTe3|D1YlrT0bwVHaR`g^T(brt^S>;DeF|DO+UZ@s1W()-eT"
    "ar5$P`M8R+NNX>AU;M0+v{Z2w$gtUuA3pqK8h?!PBva*Napl3kM6Cfi_k;o-XqByr=G0s7re0hmz`|BPOudbY5>*1%x%veB3KINR"
    "s;Vw??|qT0tFYLq++YJn4IB4$nkCe!%E0DxGMQoPRan+}D4KzBt(s=gaTR{rr11vZNsTl5dKK=XY7?rbR985r&@*h9W!!Rw0vc9A"
    "oRa|fwggF{Dq14fA`5_89RDmDp)mxCf12UT2F+Ggn<82IKmFzQgYTtl0`^p<mFNHa+YkPPkbg?6jpt}5AT3wvRuy%%e0ekRA`R5_"
    "uE@2rv>SaQuDE?x0@=W_g|EC9OaKUHDh~jB;{C~MA#6-3@bq(?Bh9yHWPxw+dR0+)SyVnK;a4UV)i%qDDA9;{mMFA{z^|+I^}qO&"
    "3F|n{iaJ@tXazI!ptPgsM!9iOCiqGK1c+T!Q3i6|t)||KDccUIX_ID3dQ(m9UQx1SjA7z^$NjBfyXYBN1a!UW2K@y77c?#YEBMIq"
    "U6aXq<xik{?eA1cd@o<z+<ZX~d;ELwTNxTsfG001U@gsc73HxCggDqp8dno!R$68cstSBv$7sbs3dF&oAIjRA-LZK|^T#Ml6Yqyc"
    "wT%h*7B`jJf=bhTU4ZZoU4X8$%AZV!nnN7nACpQjXYeACOx<o6Z%K?_8Gl@W7$EFFEosgO&E&Jh#VN2@jO(&gc@-MNX-QH`D1>d1"
    "S`fyCOySdjJPvkLD9hy%{m#4jTuIVAs?s92_%m|2B!Fwz_Q_JI$(GC=1Y&hwh%x5kO_V>V)6B`MHd9ZP$`hGIn*ry2{FCqu3JXZD"
    "2<}0w8GLQevWB?W?lM)Wlbq6gezo*o*iK7GngF1Sx`a7~d8xtkq)GTuJwq6awu+xVrg<V<^<M-T)tLHFHyIh@h4)QY<#9+uSzwPl"
    "Xk$-C41Z!XN=WaR^vV$BMnh$F9KYJ`s^>0t(c|7v6Mx|nnR-6z3!3o*z9F3ks3CgjMuwm&qg-QtBLYV;y|NezKB^qvga+Www*rcQ"
    "g}@2W9)=+d{)*841Kye`{SA?sQM?3k3~7Tx5R1lzAERq#-dH}4A;ULc2*HUN-liI?<iR%e81gcBztbqwG9wa*K%Bk*8Y0TB0-gKO"
    "ZkMGoy%Tecco4Dt*EOUgW%>_(Qx*{4zE^;=^!CK-!9Nhf`$(g5{7qHu^kP1bcIm8@eg-e#JkC;JV*dEGZz}-BY6XI5TRv0lfn;?C"
    "u~p@iA?$ro)Qn81nQUV&jdBj{yNqopVLzW(`+MZ~Fei{#d!jlRpVd<oW8Qg(PRLthY{CbK`-~mKf*a+`yN%O^-#KCsn-g!jY&l(Z"
    "9U#%7sPbaqeW%{Ln-${&E%aXZF@vxSfeZ>_LA1S3AL^pkzN;>f)X=#@8Y_8y`EAyeU{O<5=Q+fZ<Jy&3((}DM9p>>m33=?RRELQ|"
    "r<^R;V3?|_epEJtqPm6P!pT)QLlb|@5MUC(w=oR(<Ys^JudNs}uyYfKtc;+@*Xctq={rxF`5uwo{UJ^Mu!43%RhRV6=pnn}yb0qq"
    "fx0<X$Ep~_wkvy(lMrGu$T7ajfJs;RL&<dzlQ6jzVgc^<hlyAf!}U1eQHFy7{QZ27F-gYvS!s^Uy`xLZT+uvAsB{!shmdpW^tmE<"
    "5yOSOfUixVD+&dK&Ekmss_9&EH|y23c7TDw241rH_l`Flm&A;7(*x7m$M6~Qb(nP=fq8=@qQg64ng3#MP@qdS;k{8p$a-sicLP0f"
    "y^Tp1+gF_ZYv%<|avgGp26k(E=u>`wzQ>QlyhUKYs>}??dAXB|=#8A?*deC|?Z_T>A8}{U-H4hL+bE@I%&0>vP@m!1b_D>Z>K6&("
    "Iyi%I#I=ph9}Ro%&7cUyy)udX1@&ZTP27}Rh+JLCD%U2`n=HMTJaLU{KYvp(YRKf=kVm3Jitbf*c6uJ=&jE~Mu$;3};tw040dbzp"
    "xQD6_-}_2fp}{S;n34fjlL1pA-XX{tS5-c@glSzRskb{Fw5Y&2fMzg%)=P{G@LfS-6;(y4mw`V;-7heYP`R!#H`Z~Q3S^XJ#V5#9"
    "^5wr_aAq#m6K0BD@ZovK(oMbpLN4j~%)(<_2QsxS|IJ)hU`b|b&YC~lDN@s~#2evi*g})D)ZMI15vbVXOmCu>-+UYV<`XoNOu1Q0"
    "n{Cunl0M+d;G|)>c@@nA)GS=PPNe`FlV|U*A<C+9SEf)tu~gu^hxEWN_@bRUhnR1^d7Q)!*MH4e=B*vXq3QQ}@b`M~_j>U6da#w2"
    "8^U#}vV{3?asX2u2A$l4%ZpH#F?}ml1Pp{xMQInagWo1rCD|pJCPI$<mBR-str<%m@7F3g8|wf6>P=DV*j$)$vMoVd^^Vtjb%v|*"
    "qB4iVox!s@QjuEY$CqC%piV{n8&<Nt?A-~}%`{KFj%ji5XblqzdE1;9U3jZ+<=T25wC!8DFG3I0>O)YYu!TKgcTMxEb<u;;93~?b"
    "{xG&n3VzR^>(=CEbEIE21HCHEYvoQl;uyDWrfA^@iM1*p)0iG1bUviHYzC8CId?sw&892{?piu43s(ornHB~qFQl9Bw_ROmpZfLt"
    "-oFR0WE@gRf!MOvmYIc(##O+=bER>6SC;O&kfyyc88R0t<9~Va-<ra>55qBSJgJ~0>ckZ&ekqK=sHaphRF!e(b}OA#^g!OlfAjv<"
    "Gs_rfx&}AK^xT5AFfx^zQee78W$+8xKm1>>mW<5`Et+&ge7kyLq|`tVz{A8iV_by5dJ6nlN||x>jA$QI<b`_cstB9YE%0o>sB^aq"
    "v#8GFjVi-Oa6k8%qB(s{qIu40O(O%2a#M#+&d?w2<`2h>gVDEUAzD}sFCv{V5Yaa{SZsid6nX6W>UPQI7pyWd+k-1iu774~qkh;d"
    "HT8@^#i^Dl-@#;JA;g|J&Eu?2=utg2kC>Xlq}$VJ)~POVu@19lf;)}Pd+RQTnVE5+yA$MUV|bqWOJ`I=C&kBIRy?-_G_FFBZ#60h"
    "J!!CZP%<uu(K?`Tiv^4?2z*bfA&_s22P9TAh{9#1@u*T@pymeJU8`*<5<xoegAzE^wTuM2O+$d_l-71H40Wq&JfvglX2ffrMY7)6"
    "v%B7C$rW)b&Z@TGFgpXKwp7M|bXG#amhJ+7j*Ga`jvsf6>#tWx9ly-LnnZ_KMf<V=gIyPv?oZb+{u-6DKtZ@rA3N+s>_9?*H0SbS"
    "&A>@^eYAAx?tvjZwbm;wI)|G`5NR7(&@<?J`S-Eh+bCZK(@=jhR=bk#63Db!f7FX3j<$WV8JNQ)KD~3ijWqg}r&Rf4S{6A5P|}%b"
    "mUL$K^>t@Roxclb<GZ+kF-mlqay-7(g_^S=T{@OEqR<u~3vT!K1h=5ah39mAyDPLBq#%8tkG!v4o9XeRZHB2q{eQ+BwAMHpfyVaD"
    "EQVb(brKZEFWWM@<Pb_^LqH?uz~*IlXG=gik;JEWofR3GsgwI!rxyvSCXtxLNtK2qEvYkwTNvmk4YflbtpSBBn$FA~@s9>{hI9N1"
    "ODN)LU?HZ591kJegpPPm2hOopr{|3A;|6}zD$lh{gUR->XIiBhYR!sk38qzSvLS`}f)QXOwc*_XFoH6S$})PEG!!QdPq0>-(euQ`"
    "daYF@M~QE&L&WYuTRJE>ViR;~9FOToRlw+${4qK61TdaxZu|)@1!EH)jC^r($&pbMzkBYra}<*hcNd<S+IIug-1Cs8SycAbvK?(r"
    "z=etBQ11A!iOX49%n2X=J(@p?PETZa?V^N&(kMf_(E1auAGn)3WJUHeORtm05B3<8ZaUhm>PWbt@#c#ea^|iMP4Z}#+3dgA%Q_5D"
    "B<M4(eaXGgax9EK{D%8Q!_}d;(MZ^`;vvnC`^d+a^BSf}&k2^+kB+CSKkypFwQ<!wn8TE$U=GYSA7PiJYAN%1|E}vz32->(_uolE"
    ";qEI_)0z9qG)XM?rTc9(_WG7Zx&A<+ESNYa@Tk-?HNWj`s$e^>Lf})om(<Y}cAl#$LFp<>8ZS{74iRm^OO7|6c=zt8oDF1G(19s6"
    "pcj=E%v(!FZsQc?N4<+;UhX)_Wdp8X{eAgGmTuFEE{jO8xH-+dOK^Nc!-aG1RPeQgOQdeu?uxDPYFAC3e2AhwaN2>m6Mj~wB-Ndm"
    "*mg){q#G8*g>$pw0=ev9+2jEoT2@9NAm1_7^Kygrxkj+(&$Dk0Iabh{Y+MU+4i<FA4$YHO(ePAR0ISRHT?`wLYeaTZ;?jaz;OKlv"
    ";$oYG5%{<vbPa%Y1i=+@w|FC5Y6bDN;zh=hlycqiw0m)Qhq(-X)t_K9!@VhX)+kP#el(t_lMgixYV}!XDjS7#(028HDPC4aFLxsi"
    "9bKd>>m2LGZPZ+?X?C>jo1O*++)=cv@W@nASG&5BGgDAB#2@**lyg(k*pQr#W*%^=(TZ-`3xm|Fs|1E!aw$@n^45@5sl@LJ+9K`V"
    "hh*8S=sqlqPdZ>(%t0+)>s`Kd3mA7<D@$`Kmw4n%LwZqRn+_(nyDG`Jv4cSZ|F509Z=Bcz9KBew%q*@U$};IYzcw){0l5Z>6Zdp6"
    ";l8^Wy(+~0FII=i(50>L&0l`_kGDUEufM;2`~HWw;ZLtW{D@bc0|NHvk5M@X3z{cI{1GxB4IgN!C)(&M4$abgyGd1X^Hm67lYZ7<"
    "?1_9fc-w7Z!gCs`*5x^nLK)e`jl2jZwoI(YRw5%p+t<*!PaR>2bP-3H(;<{}9_@w(v`sEvo7~mJ>_Z@27~Nyd7RH&1^1wZU$hWp}"
    "D}gaKJNLAnZ+b1<E&$mo)S31*?*O0W#4$Y4=GBnFI6MaJ@l%(jp|b=MtB;9y+;`+qRAOK+%4hElA@K-F?3VRX*-q)dKS0enW#{N%"
    "_$4@z8VV~v{P~@iMe#=s?p%8~P#M?JOoZi>*b#}>4(|NcUTF8%zmDkdbzWRo01@*yUQ5B}{T`DkZw5hgHMz{q$vUKU106w5U0Uv>"
    "*68ax5R^JuS2fK+zkyx$cW+RJceKI*5AVT$K_+z3nmj4J(X4x#Otjr`pS-LBr0IAL(QX=ft)+FP+{}Ef4g=Z9(2}aT;L&o`MJQ)`"
    "oT%UO%a)-(MdfzLFz9Z1&kSa_2tWHXdZhOdGjA)%cE~Dbys2hVH(nQNEfA(zNG)-zwMrj>PiI%dSW}M^%M)k%l%3Ary*V8ITEPRV"
    "Qyxj-!sVcmo)wP=WAYzQh2t}A%LwU`GkP2k&Bn60>E&uPKA*Zw)DQQz;)ijnteIBY^v-(|J0y9Xf6R+dc_TTE*Hu(0e=NOia<QIq"
    "<f_8#`16bd48lS&xt#iAk=0wrG97Z33EBi@)X4dOcDdFHD36fCM)#Qt!E7M3Kv42cmYQZ7UdH}Od@D>gKqHaTT0d<-Lpg$xoKtds"
    "Frz3i$s*}cob2p<TTwxD+p>ypJ{UzUilv3Q)nsgC#@*sRZyBQHS_n1Boh|k6A<lvw+_Ht5J4t&Vkme{Ruy>2A=J<U0t@J7v6-%Kx"
    "rrI2QGCHx~Zq<|HR%L4|H=_l`JAt_4-jzi6o#OPt8UK?aqu(D!=Rolf@_fz=&{z?RglJYJuwzCyfWT!RJxze)aB66L%K&gkZw?w9"
    "cCj=NnY+uaIsHu1*lx6SCbY%!BQ(C3<E$6p8rT{%Ou7rWE${sWq)RTNo;L}|^s$p}5-KcSn^-|Zk6bPt+9=#*QQlsO(%p+s?T-7x"
    "_KrS>iUl1c!#vxUH3TsU{#77O!F{=~q@%S8K*a1G-esZVT;wyQ4zpw+jlIbQytnj`7I|-+%Pf@w4VUygV1|*n$TB?2NOW}1XRD_j"
    "^s0n5D2A^~;Z}Y3j+EPG?m!K+wwwL!{#o<YK04jmMw@!QgI=8`JQ+p@Q+saiFt!oU?oT$kzJ_<I)d?mQjYj(Rf5N-sE>)48>4<h^"
    "QCR-kY=GD%!lBrX&JvvQ#Hz}XCCsBut$M<6&cpVeuD$VJlMAptv706<>KF^&;v;*{qV0VWEk>^EP3+m~j~U7<CI^dZlLoDb{we$C"
    "Q&}PzH20)E&~L7BO6Y{B{g+?LYstNn)2KRZ^2x{Z_#`%(^2K4KqoHF5I42GVymS6*(;wgEcklHQ6YPEPC~`39{^+EQe%81mn&z|n"
    "dc9U9|4QAr-+c4OfA0Tm6#qaSc7A(0)O232-p|xAL#B6m^7rb@m~9kqQo1jPKgk#Hs-HAolMAF>bI6#MW-tromaBXyBF(G7*_V$>"
    "KvaSIsp+jUzXCZE0s&g0Jo7aS`GN*YTBi%OV;)~l+H9ln5i%UU)%o&p=H29zfr-WOx1hJ|T0iG89?Mu~>B9!IS6mE|WpP~5&eT%o"
    "9*htVfYB7*(kkcMCn$tpPOh+=p!gAnRKtg4O<^d-p4isa{{pY}O6L"
)
PREFLIGHT_SCRIPT = zlib.decompress(base64.b85decode(_PREFLIGHT_ENCODED)).decode("utf-8")


@dataclass(frozen=True, slots=True, repr=False)
class ProofTransport:
    """Secret-bearing inputs retained only at the local proof transport boundary."""

    cloudflare_account_id: str | None
    cloudflare_zone_id: str | None
    cloudflare_zone_name: str
    cloudflare_token: str | None
    dokploy_api_url: str | None
    dokploy_api_key: str | None
    coder_email: str | None
    coder_hostname: str | None
    coder_password: str | None
    tailscale_required: bool


@dataclass(frozen=True, slots=True)
class HostIdentity:
    machine_sha256: str
    ssh_sha256: str
    architecture: str


def assert_namespace_identity(*, host_a: HostIdentity, host_b: HostIdentity) -> None:
    """Require two physical hosts with one supported architecture before upload."""
    supported = {"amd64", "arm64"}
    if host_a.machine_sha256 == host_b.machine_sha256 or host_a.ssh_sha256 == host_b.ssh_sha256:
        raise ValueError("Host A and Host B must have distinct machine and SSH identities")
    if host_a.architecture not in supported or host_b.architecture not in supported:
        raise ValueError("Host architecture is unsupported")
    if host_a.architecture != host_b.architecture:
        raise ValueError("Host A and Host B architectures must match")


def assert_followup_proof_contract(
    *,
    contract_name: str,
    receipts: tuple[str, ...],
    host_identity_mode: proof.HostIdentityMode = "distinct",
) -> None:
    distinct = {
        "upgrade_host_a_contract": frozenset({"host-a-baseline"}),
        "final_proof_contract": frozenset({"host-a-destroyed"}),
        "reseed_pair_contract": frozenset({"host-b-clean"}),
    }
    sequential = {
        "upgrade_host_a_contract": frozenset({"single-host-lifecycle-baseline"}),
        "final_proof_contract": frozenset(
            {
                "single-host-lifecycle-host-a",
                "single-host-lifecycle-teardown",
                "single-host-lifecycle-final",
            }
        ),
    }
    match host_identity_mode:
        case "distinct":
            contracts = distinct
        case "single_sequential":
            contracts = sequential
        case unexpected:
            assert_never(unexpected)
    required = contracts.get(contract_name)
    if required is None:
        raise ValueError("unknown or mode-incompatible followup proof contract")
    missing = required - set(receipts)
    if missing:
        raise ValueError(f"{contract_name} requires receipts {sorted(missing)}")


def remove_authorized_outputs(
    paths: proof.ProofRecoveryPaths,
    attestation: proof.BaselineAttestation,
    *,
    boundary_hook: proof.BoundaryHook = proof.ignore_finalization_boundary,
) -> None:
    for name, path in proof.output_paths(paths, attestation.output_sha256).items():
        if not os.path.lexists(path):
            continue
        content = proof.read_proof_bytes(path, 16 * 1024 * 1024, 0o600)
        if sha256_bytes(content) != attestation.output_sha256[name]:
            raise proof.AbortGuardError("generated output bytes are not authorized for rollback")
        try:
            unlink_exact_regular_bytes(path, content)
        except CaptureSchemaError as error:
            raise proof.AbortGuardError("generated output changed during rollback") from error
        boundary_hook(proof.FinalizationBoundary.ROLLBACK_OUTPUT_UNLINKED)
    expected_result = proof.result_bytes_from_attestation(attestation)
    proof.require_generated_bounds({}, expected_result)
    try:
        unlink_exact_regular_bytes(paths.output, expected_result)
    except CaptureSchemaError as error:
        raise proof.AbortGuardError("result output changed during rollback") from error


def complete_resumable_finalization(
    recovery: proof.ProofRecovery,
    *,
    boundary_hook: proof.BoundaryHook = proof.ignore_finalization_boundary,
) -> None:
    if recovery.claim is None or not recovery.resumable:
        raise proof.AbortGuardError("no resumable finalization is active")
    guard = state.read_abort_guard(recovery.paths.guard_path)
    if guard.attestation is None:
        raise proof.AbortGuardError("finalization intent has no attestation")
    result = proof.result_bytes_from_attestation(guard.attestation)
    proof.require_generated_bounds({}, result)
    write_or_verify_exact_bytes(recovery.paths.output, result)
    boundary_hook(proof.FinalizationBoundary.RESULT_PUBLISHED)
    proof.verify_attestation(
        recovery.paths,
        state.read_abort_guard(recovery.paths.guard_path),
        require_result=True,
    )
    state.complete_abort_guard(recovery.paths.guard_path, claim_token=recovery.claim.token)
    boundary_hook(proof.FinalizationBoundary.COMPLETE_GUARD)


def collect_coder_array_pages(
    fetch: Callable[[str], JsonValue], path: str, label: str
) -> list[tuple[int, list[JsonValue]]]:
    pages: list[tuple[int, list[JsonValue]]] = []
    offset = 0
    while True:
        raw = fetch(path.format(offset=offset))
        if not isinstance(raw, list) or len(raw) > 100:
            raise ValueError(f"Coder {label} API returned an invalid page")
        pages.append((offset, raw))
        if len(raw) < 100:
            return pages
        offset += len(raw)


def collect_coder_workspace_pages(fetch: Callable[[str], JsonValue]) -> list[JsonValue]:
    offset = 0
    expected_count: int | None = None
    workspaces: list[JsonValue] = []
    while expected_count is None or len(workspaces) < expected_count:
        raw = fetch(f"/api/v2/workspaces?q=&limit=100&offset={offset}")
        if not isinstance(raw, dict) or not isinstance(raw.get("count"), int) or raw["count"] < 0:
            raise ValueError("Coder workspace API returned an invalid response")
        page = raw.get("workspaces")
        if not isinstance(page, list) or len(page) > 100:
            raise ValueError("Coder workspace API returned an invalid response")
        expected_count = raw["count"] if expected_count is None else expected_count
        if raw["count"] != expected_count or (not page and expected_count != 0):
            raise ValueError("Coder workspace pagination is incomplete")
        workspaces.extend(page)
        offset += len(page)
    if len(workspaces) != expected_count:
        raise ValueError("Coder workspace pagination count mismatch")
    return workspaces
