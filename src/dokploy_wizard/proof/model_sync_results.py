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
from dokploy_wizard.proof.model_sync_preflight import with_cloudflare_fingerprints

__all__ = ("REQUIRED_RESULT_KEYS",)

_PREFLIGHT_ENCODED = (
    "c$~ExYm?i!?fd=;RUhu8i`U-d9__gCW%6p1={1+h?uT}K9*tI_*XqiWPn0&<^Z36P06~hB<X5K6Ox6+!;zbYuLFyt~Z_29n{v*p`<dt&dZSuHYmeqRX$yHNlIs7)ao2pENgkJe5FP2rg_L?fsvs;gci>fLs7#mkojJ!(x-UwM=ME`vGujtjkZ{A*eGw+%Ao%i(W*$@14k>s(IUi6{-B+{%BNgeR8@w=OwkGk<)T%@_E!s&$v|5C98<f<oP@IZ=uNi;{^ax?OhG6fda@^0j<M4XBWxGuzJ;Fpo$ze?0iRd^rDLR>`UMid$wFsj+OZL&P2PEiFmpJ6!0){CfWiikA><61S1q2nU@yvmXlwv!si^m-9(;(8T{FPW@yOex3MFwRxW1q!HHiEvH=;M)=;jq7-ZT(c|yYH|FDH9}(u6#tmvGlOO=>Q$M}{Exq0-}qj(Bw$bEQh5H~-`@BkA^)7!E6>qRKw7S|wJ4i<_UtP3VhPmcrYxkev>SgWu2lQD0<wW+3s1bKiU1JKMG*ja=>6=q5H_X^c>2^7Nb@ZkS>S8DUR{=69#?mQ`Q=GPwT<&KP9<U<rvfb^@SA#h`4>M7m5!6VY|<r+7BC}sLOOaj%1z2D#U%j{Aa+^DImk6%jJ&5KWjmy%RhA3x%_OyZWu+uj7((y4>Td<x#E-}#pzBRH=qK>Mp=t47z)yi|jVI@YA42!i--wF%o;|&~`i361_;=^GG8j^TrzmS+Eh}Ul7l{a%IM_&*)FCp<E#o^;2fl0)v|=Cy;^5Gms<CEw-@IhSL!4)+_u8n|n1F9_Q;RjIG%J=R2=CAZ=q9iIa5U5$;RyebRE#-?g+wxSyJ5T~F}^DNNeN<ruz#A-oDrJvtHi}Auvko*suD#VX~StrQcJ{yZIN0Kg$tR&rvZ5!?5I#BmrL}yclEWBWJO$OWnuBB<#0&=*RJi8rBahEnL7x?>MV$T%q6S1xD&^j<E1thUusdLJc~91&U*Zrc?N|Aq*ny@Al3{n+moyz"
    "Dc74^)Z!?oG@nmq-c#FYF-aW&WZ6_O$1pDqc%Cec?!_a7v3M=i(|uN?%vFCO@F-*IL)~O#3NNfTSyzW4HD!i9?4Y$hX)*lJW|WZ573qa0sv0$w#bNwny{R9&*jbNTKTG|oOJwBvs4r;7A9zDL2FMWIbt6MiS8*XRzhQyngqAFZf_qWGN-zL(-wG%U76B(fdl-f=_zOb&2dtVA{f&^By?6=Y5K@DJ5wpgbAFXS}ytaHCLx!(k2*HsVUS|@l<jywr1oARi-&veXo)Hd2AkNl*2@z#egU<bUv&plB*2DxO9z-nvWdrF*mHnYs$`s<;D*-qwZ%e%H{2d{@jU_6_U)A+SPA8LilZ{*H$FKk=NuB`{lZPLCTLCCmD-cB6>XBj(B&%bHt)id|Ve7M^#$-ZfvbDJw<?PzEjBO}kKc86oTjX~)A&?haqB<C##g{n2ymO9D$XjA;!Uu@^3Oj}cH_GX?jnjtjDq;|up*NegoG!W!kZ4g<c{;G(sds+0Q20O#z2&~oAS^>5gMwHPuWz%vrfj6|stY95bmmB7!OK^zW=RPaHPuZ~KrA_|U7jU5+49qFk}T6mjeU{HC>7|G)7cUXQ*_mj%2uGLZYJ1DOY<<Z{IP-nlK{T8VZg6y_QSunVvNDgbsVxXg0fg<cfF+VG-)PVL^l66OTS$}JE3YSS~GIUu2kNH@tQ#09IInh3dFW6dytb5Vlt>=yvcw`SH)eW_Cbn-&aDs&aJSusY*h-b$AKE9IT*m-&-WB2&iKA6&EdIsbZMC@nnwjY9f8)t<Xk#^t_YsRaAD8jYgNjcLIGj3IKqFXbEdkLUUh2+7#KA0oXvl5yy1RH^f)&?(5-z8Um;(IS+-ht1#v`&HDa0nbgNOIOV#1sC?QgMYkjwY4qb1f)5Ug)EC1SA;AtTvm7#&%+8+9t-=A#pXE$jP*h^J019G14<YHQp6C69_)Sw;N!|ns_47!b|X}OLwipC0cWCiLIJlm!O;Otl|JS>Ro;55cPu5I7^(Xhwf"
    "46;zFmnTuZpq?}>CzUirR9={t_nW+YY?DG*7ovK|0Du?z21Y>k0V;@#N7Bz+(O+Ot_8EcivHd{wTjM5gF@^<K)e_4sfrgrBXxH8hMIuNw3rJnvjFCse1ZELHbkaq;7lyi-A{H8SE{J&bGfUR1pUr!t*-OCS(=y06ik*S<k+VHOI<CM0Wt+gCpdoTQ{>-PBKP-?szKp>RScjMy`W!^C>&$fY%O#AzMCD9T5S4uHE7%Y_PduVI*;M6!L{g9AJnxz<cgb$i11oR6VAv_pkRaT)XV0h6_oCq;kF1r?gK4PW@sMpkMoWevam_Hr>A<RF7h$bR1q{mNr}xh4<VHWVvf|<)tIA>xMnO7b&63XSzP|1>snatF8{gUKgjS-<l#@ePU8p%5(xqcrBMNN+D#7jkp5PYre&O*SRw_y<1}Sjg*CX$1*JgTuc}_DmsQ<5+gVq{5NyhQmZ!zqesS^?%zii9sl0zt#4FR>70|{6+Ix{HH<tLXJGgC)nv;Mb#<4YM9$0VvR_%d0Ew8_QI;|B95Lv7bbt3e^dp}PM8|ENL7IL9x1XeXWqB76@y9;DcW4tP%o&aqaf=Y$m4!0)xnQ!P_tvVH7{R;h<tv*KEUZe^P^q%dF50vyV0&<Lt1uB!Nv6E>VQtiso;y=Nljaw$d4$01kNL5O=;K*i2IHbEy}c+?ozfkHP^KjDd|7<ejU{0WvfunCL1Pp{4?d!zW>+I^>-Oo+S16h-a%05!L!tD9M6PiF0CYXZ(pEQfN(yH!$+vvNZC)Zc^oBkS}?cGvC$1#A&=v<oRegJhjTC0UW#*>#GP)Jfw9do)Tn9W^^w4i_|Dzvv++Rw`+d_hy-<rieaClT`p^iawJL71L(Dk>D<s98;|i-*7)QT<yX!dslt%NZ9i7E-McE$cJZ>2Bt|)2o_5xN7B{TWdSx?Uusv~gV~Kp3dMmgT@!XL@4(IofVn2!^`-<k9P|5Yl2Et>18O=gA61j2<UY5COk*#vl_-}tB+66~R|z~S^+e6D"
    "dxb)5k217)d}`~X9bIAPsj6a>uCloC9ChIk))p*syuG1z;Roe3kX=Csrc{HTwY5;ZwPg54L0R3)O`NF0z)>y@xP0;Z?3+AWXEhyobFjEN&b&)-|CyKz=bQ_xqt$byZrSdNE%ACvL!NwyqCIfbftWKti(`_?PE2e&I5N^HF>&FX4xb^H9V|^A(4l2z1Pt<AQ9Ug;Se|MGTlIVL%?HN{dXtSOBhF!2XY9~CIXfCPRTjYFltiRGJ03P5*NE(-#H9s$frInGiHmI#THwQm&@}+1BM7dLyCo~;QVNK#wJHJbNhy~dPrGG@x0~?b7ySv=Gu)eEr$%w4f~!4ICm%8ns&fi;VqMrMq=UAr_jB<oW%P14($Jnvo^K8`1@0TyvAG^#PJ!&(Y8nRIQM>^}A}X7D)6_Fe-l1_NlKa`;@Wjly?WJvqPk0m$IMrxHSM8-I>Xmg0!_K+b#Y}li$f`u@cLi;ccJFj4+3WZ=s>;tYP_m5pw11h2Je9L&?gqv!Yvox%+a)z}EFrz9u}ub{?XD^^ZtPGXf&W|Q{*FrQ0ge`yBCTCRoafwk`rbsV1mqgnoVX_>gnNFq_qv1Xf4bOp6stBV(VJgi|Ksha=;f>Hw;x`=jXu7-d570F0|NFZ4{<dC3!0>5at|4hgdeojBW?7GL-Xv`ZW483UYP(k$wvvsp7O83t2Qk(5b+#`YGic^BvVFq6G)<CnAj?{9$SgD2yLICbDKHB66rFDbEktT$s*ni4QQKOyf(RuP@h8}Tom8po-Imp5f_26ZJJVb^AwK~6voh8%|cVsy%ug4fNT}^nf6uH0H68P1fJ;VYsg?69)tFVlFL#{aN3R4hr~PVI||rTVqh<;NAC?G@d!!cZtHnxJEH&o05#|4h@*qyMQ|iFWL94P^4`ni<X(b1mmUrj+I5(Tu$+=|MC`SLx!>9go%i{dG5x(P%F7xc67{BPDfqnIVlw58A!shb^V}TQA+1}q2y*P=Whb>pU)O=4)XBPTrZ2C|=^hxehbK<J"
    "UEsfegEG9Q3I{xV0RIJ<&{;KkQo7NseJRsuyW>7pSp`U=U8w4cX;ihA)|IMe#n+;Hri`;VR8rN4)Gb$CgsO0|bE1CBud)pJIj+`2hC%mZduA}cM)>hB=#f4^%)G84+aaqMt3x%Nx~aNQYk@G$OlpRwr*-xKd^)EZ+M0TtSe`hS$Lw_c{>^UpTMZAWPJSeXGna!#dQv<djLF|03&$tgmKM?_r}el$G;7OJP0uW&>cr>PWes15=!bh-@xwSpZl)Df0V4G#c1ZH3xG&1jg^`@b%Q~(__ps2Uw<Z_s8Al;%%#Od#IKUt*6yf>QAIiL0JC^B?vrNz?$fLsH)VnR$S^?z&a@hDb7ZI2Zgcb-&zRps!T*6}P-!8SnqygF!DXsdc0i|tpRzc!?!j|(!kD|cDMbe=-**W{Rq5|u-WffOG7)8U1rNZ1I+*g@#kGM}OL$q8oA%omGQhx<;7VO|!S*Rk6H-iCbfnoxC*H|^j=iUEHuYB36Vw(F@TYyhSCl<^XJvpvLzO-^PDk#nwVy=1@9NjlUr4P>dkAjSTyW2YlLVZ;9H8VhCSu7I5R3)%uOn0lnWgomvfa7p#Xne~6a7QZ#4Gz1MG!U7)<<^2e_1bqDZQYY=vAl!E59*@qIk*On1~rpz0k`G7UqCwNLfvVTfJ{54H5@7|UQMi^p$9I=4ILD2^0=UznyS&=b!`3i#C>XeM_+}CDPHrSgC?JA2y7DS%O;hA`+Nys#!C@^i1{t7Wu{YC_MIZ$>L`{pq_#Kl1&<26xAc>hMQ@zTENul0m-H>8W+W-|96vTBI<n`p#g`5Aij)p0hCczoqx$Z>f45EF>mO)s5BuBmGxKWB+DwZ%h#Gmli(Z{3ycvddvA&zz?K=o)&nHc;FJVo!I>DsEXryodN4zVZQkD6Mj%ZVrrRA^n0f=oPDiqt%nS(Qog_w*iVIHmbsz(gxG;AN}Qx5f4=K{)}l$$0i>KF^yoU->kUf-tibk8R!p*>suF++JpxU;C5G-yHekJ-Pv"
    "mBo@lb5GhGeK)F7LMKFRzx$4trRoijqw27!Za$vIC$V9+FAgIe4IR3`89E%Olk@*I{r#u>j?Yz0DDQ(;a)UwlM>lQcqr{46R*Y|(<x*7YOWU7*{PFug_dk78AK7B(Psc+|XL<ELwN(u1-r3P_ZuOXToUAhX1_VDV4ph~T8!yQP(y2LQOfx-L3FU5AX|k%Ur~~I*KCS>!4eqC*Ri(dVA2WdfEs>ui3XJ@M21;7@rw2Sf-?UlB(F0^S>Pw|(JD5i3L=P*udM*X*x?)KA<t*?=xR9oxf@A(giQ}(9Z~3KsEE2qyvCOl(6=ttk4C1mlR<v_5<GBYT#0y|Fh1XQ&eES79!q37B+)hyZh$7m<M`TS=#G5^~t&9Hxj$YiV"
)
PREFLIGHT_SCRIPT = with_cloudflare_fingerprints(
    zlib.decompress(base64.b85decode(_PREFLIGHT_ENCODED)).decode("utf-8")
)


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
