import io
import json
import os
from datetime import datetime
from pathlib import Path
from typing import BinaryIO

from dotenv import load_dotenv
from filelock import FileLock

try:
    from openai import OpenAI
except Exception:  # pragma: no cover
    OpenAI = None

try:
    import pandas as pd
    import streamlit as st
except Exception:  # pragma: no cover
    pd = None
    st = None


load_dotenv()


# Grava na mesma pasta do projeto (ao lado de `app.py`).
RESULTS_PATH = Path(__file__).resolve().parent / "results.xlsx"
RESULTS_LOCK_PATH = RESULTS_PATH.with_suffix(".xlsx.lock")
RESULTS_COLUMNS = ["Criado em", "Sexo", "Prova", "Distância", "Tempo", "Altimetria", "Entrada"]

DEFAULT_SCHEMA_NAME = "trail_runner_experience"
GENDER_OPTIONS = ["Detectar automaticamente", "M", "F", "Prefiro não informar"]
DEFAULT_SCHEMA = {
    "type": "object",
    "properties": {
        "sexo": {"type": "string"},
        "rows": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "Prova": {"type": "string"},
                    "Distância": {"type": "string"},
                    "Tempo": {"type": "string"},
                    "Altimetria": {"type": "string"},
                },
                "required": ["Prova", "Distância", "Tempo", "Altimetria"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["sexo", "rows"],
    "additionalProperties": False,
}

DEFAULT_PROMPT = """
Você está organizando o histórico de um atleta focado em corridas trail, principalmente provas brasileiras.

Objetivos:
1. Corrigir erros de transcrição e escrita.
2. Padronizar nomes de provas usando apenas o nome da franquia da prova.
3. Validar ou estimar distância e altimetria quando possível.
4. Fazer uma checagem de sexo/gênero para uso cadastral simples.

Regras:
- Foque em trail races e ultras, especialmente do Brasil.
- Corrija nomes de provas quando estiver claro que houve erro de ASR/digitação.
- O campo "Prova" deve conter somente a franquia do evento, sem edição, etapa, cidade, distância ou apelidos.
- Exemplos de "Prova": "Boi Preto", "La Mision", "Indomit", "UTMB", "KTR", "Brasil Ride".
- Se a distância estiver implícita no contexto, padronize o campo "Distância". Se o atleta citar só a franquia (ex.: "Boi Preto"), deixe "Distância" vazio para a etapa de busca web preencher.
- Faça as checagens de consistência de distância e altimetria internamente.
- Se a altimetria não vier no texto, deixe "" para busca web posterior.
- Em "sexo" use apenas: "M", "F" ou "Prefiro não informar".
- Se o usuário informar sexo explicitamente, respeite isso.
- Se não houver confiança suficiente para inferir sexo, use "Prefiro não informar".

Sexo informado pelo usuário:
{reported_gender}

Texto do atleta:
\"\"\"{text}\"\"\"
""".strip()


def _require_openai() -> None:
    if OpenAI is None:
        raise RuntimeError("Pacote `openai` não está instalado.")


def get_openai_client() -> OpenAI:
    _require_openai()
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("Configure `OPENAI_API_KEY` no ambiente.")
    return OpenAI(api_key=api_key)


def openai_model() -> str:
    return os.getenv("OPENAI_MODEL", "gpt-4.1-mini").strip() or "gpt-4.1-mini"


def openai_transcribe_model() -> str:
    return os.getenv("OPENAI_TRANSCRIBE_MODEL", "whisper-1").strip() or "whisper-1"


def _coerce_audio_file(audio_source: str | Path | bytes | BinaryIO) -> io.BytesIO:
    if isinstance(audio_source, (str, Path)):
        path = Path(audio_source)
        audio_bytes = path.read_bytes()
        filename = path.name
    elif isinstance(audio_source, bytes):
        audio_bytes = audio_source
        filename = "audio.wav"
    elif hasattr(audio_source, "read"):
        audio_bytes = audio_source.read()
        filename = getattr(audio_source, "name", "audio.wav")
    else:
        raise TypeError("audio_source deve ser caminho, bytes ou arquivo aberto.")

    if not audio_bytes:
        raise ValueError("Audio vazio.")

    buffer = io.BytesIO(audio_bytes)
    buffer.name = Path(filename).name or "audio.wav"
    return buffer


def transcribe_audio(audio_source: str | Path | bytes | BinaryIO) -> str:
    client = get_openai_client()
    audio_file = _coerce_audio_file(audio_source)
    response = client.audio.transcriptions.create(
        model=openai_transcribe_model(),
        file=audio_file,
    )
    return (getattr(response, "text", None) or "").strip()


def extract_infos_from_text(
    text: str,
    *,
    reported_gender: str = "",
    prompt_template: str = DEFAULT_PROMPT,
    schema_name: str = DEFAULT_SCHEMA_NAME,
    schema: dict | None = None,
) -> dict:
    if not text.strip():
        return {
            "sexo": "Prefiro não informar",
            "rows": [],
        }

    client = get_openai_client()
    response = client.responses.create(
        model=openai_model(),
        input=prompt_template.format(
            text=text.strip(),
            reported_gender=reported_gender or "não informado",
        ),
        tools=[{"type": "web_search_preview"}],
        text={
            "format": {
                "type": "json_schema",
                "name": schema_name,
                "schema": schema or DEFAULT_SCHEMA,
            }
        },
    )
    return json.loads(response.output_text)


def extract_infos(
    audio_source: str | Path | bytes | BinaryIO | None = None,
    *,
    manual_text: str = "",
    reported_gender: str = "",
    prompt_template: str = DEFAULT_PROMPT,
    schema_name: str = DEFAULT_SCHEMA_NAME,
    schema: dict | None = None,
) -> dict:
    transcription = ""
    if audio_source is not None:
        transcription = transcribe_audio(audio_source)

    source_text = _merge_input_texts(transcription, manual_text)
    structured_data = extract_infos_from_text(
        source_text,
        reported_gender=_normalize_gender_input(reported_gender),
        prompt_template=prompt_template,
        schema_name=schema_name,
        schema=schema,
    )
    return {
        "transcription": transcription,
        "input_text": source_text,
        "structured_data": structured_data,
    }


def _lookup_race_details(
    prova: str,
    distancia: str,
    altimetria_atual: str,
    contexto_atleta: str = "",
) -> dict:
    client = get_openai_client()
    schema_name = "trail_race_lookup"
    schema = {
        "type": "object",
        "properties": {
            "prova": {"type": "string"},
            "distancia": {"type": "string"},
            "altimetria": {"type": "string"},
        },
        "required": ["prova", "distancia", "altimetria"],
        "additionalProperties": False,
    }
    dist_busca = (distancia or "").strip() or "não informada"
    ctx = (contexto_atleta or "").strip()
    if len(ctx) > 6000:
        ctx = f"{ctx[:6000]}\n\n[texto truncado…]"
    bloco_contexto = f"\n\nContexto adicional (transcrição / texto do atleta, pode ter pistas de cidade, etapa ou distância):\n\"\"\"{ctx}\"\"\"\n" if ctx else ""
    prompt = f"""
Busque na web a distância (km) e o ganho de elevação (D+, elevação acumulada) do percurso abaixo.

Regras:
- Retorne em "prova" apenas a franquia da prova, sem etapa, cidade, edição ou distância.
- O campo "distancia" é o percurso principal **oficial** associado a essa franquia, ou o mais comum, quando a franquia tiver múltiplas distâncias.
- Se a distância informada pelo atleta estiver vazia, use o contexto e a web para **inferir e preencher** a distância (ex.: franquias conhecidas: Boi Preto, Indomit, KTR, etc.).
- Se a franquia tiver múltiplas distâncias, escolha a distância cuja **evidência** é mais clara: priorize a que casa com a entrada do atleta, senão a mais citada/“carro-chefe” no site.
- Só retorne "N/A" em "distancia" se não houver hipótese razoável após a busca. Evite "N/A" cedo: tente cidades, etapas, nomes alternativos e a grafia comum.
- Foque em provas trail, especialmente brasileiras.
- A busca deve priorizar fontes nesta ordem:
  1. site oficial da prova ou da franquia
  2. página oficial da etapa/percurso
  3. resultados, regulamento ou guia do atleta
  4. Strava, Wikiloc, Garmin, AllTrails ou páginas de GPX/percurso
  5. portais confiáveis de corrida trail
- Tente explicitamente variações de busca com estes termos (mesmo com distância vazia):
  - "{prova}" {dist_busca} distância km
  - "{prova}" {dist_busca} "percurso" km
  - "{prova}" {dist_busca} altimetria
  - "{prova}" {dist_busca} "ganho de elevação"
  - "{prova}" {dist_busca} "elevação acumulada"
  - "{prova}" {dist_busca} "D+"
  - "{prova}" {dist_busca} percurso GPX
  - "{prova}" {dist_busca} route elevation gain
- Considere também grafias variantes da franquia e nomes parecidos causados por transcrição.
- Se a franquia tiver várias etapas/cidades, tente identificar a combinação mais provável pela distância informada e pelo contexto.
- Se encontrar D+ e distância para a mesma prova, use ambos; se uma fonte tiver D+ e outra tiver a distância, racionalize de forma coerente.
- Só retorne "N/A" em "altimetria" se, depois dessas tentativas, realmente não houver evidência confiável.
- Normalize a altimetria como string no formato "1200 m" sem sinal de "+" e sem texto extra.
- Normalize a distância como string, preferindo "80 km" ou "35k" (seja coerente com a entrada original quando fizer sentido).

Prova: {prova}
Distância: {distancia or "(vazio — inferir a partir de web + contexto)"}
Altimetria atual: {altimetria_atual or "não informada"}
{bloco_contexto}
""".strip()

    response = client.responses.create(
        model=openai_model(),
        input=prompt,
        tools=[{"type": "web_search_preview"}],
        text={"format": {"type": "json_schema", "name": schema_name, "schema": schema}},
    )
    return json.loads(response.output_text)


def _normalize_rows(rows: list[dict]) -> list[dict]:
    normalized = []
    for row in rows:
        normalized.append(
            {
                "Prova": str(row.get("Prova") or "").strip(),
                "Distância": str(row.get("Distância") or "").strip(),
                "Tempo": str(row.get("Tempo") or "").strip(),
                "Altimetria": str(row.get("Altimetria") or "").strip(),
            }
        )
    return normalized


def enrich_rows_with_web(rows: list[dict], contexto_atleta: str = "") -> list[dict]:
    enriched = []
    for row in _normalize_rows(rows):
        prova = row["Prova"]
        distancia = row["Distância"]
        altimetria = row["Altimetria"]
        if not prova:
            enriched.append(row)
            continue

        try:
            details = _lookup_race_details(prova, distancia, altimetria, contexto_atleta)
            row["Prova"] = str(details.get("prova") or prova).strip() or prova
            looked_up_dist = str(details.get("distancia") or "").strip()
            if not distancia.strip() and looked_up_dist and looked_up_dist.upper() != "N/A":
                row["Distância"] = looked_up_dist
            looked_up_alt = str(details.get("altimetria") or "").strip()
            row["Altimetria"] = looked_up_alt or altimetria or "N/A"
        except Exception:
            if not (distancia or "").strip():
                row["Distância"] = "N/A"
            row["Altimetria"] = altimetria or "N/A"
        enriched.append(row)
    return enriched


def save_results(rows: list[dict], sexo: str, input_text: str) -> Path:
    if pd is None:
        raise RuntimeError("Pacote `pandas` não está instalado.")

    created_at = datetime.now().isoformat(timespec="seconds")
    data = []
    for row in _normalize_rows(rows):
        data.append(
            {
                "Criado em": created_at,
                "Sexo": sexo or "Prefiro não informar",
                "Prova": row["Prova"],
                "Distância": row["Distância"],
                "Tempo": row["Tempo"],
                "Altimetria": row["Altimetria"],
                "Entrada": input_text.strip(),
            }
        )

    new_df = pd.DataFrame(data, columns=RESULTS_COLUMNS)
    with FileLock(str(RESULTS_LOCK_PATH)):
        if RESULTS_PATH.exists():
            existing_df = pd.read_excel(RESULTS_PATH)
            out_df = pd.concat([existing_df, new_df], ignore_index=True)
        else:
            out_df = new_df
        out_df.to_excel(RESULTS_PATH, index=False)
    return RESULTS_PATH


def _normalize_gender_input(value: str) -> str:
    cleaned = (value or "").strip()
    if cleaned == "Detectar automaticamente":
        return ""
    return cleaned


def _merge_input_texts(transcription: str, manual_text: str) -> str:
    tr = (transcription or "").strip()
    tx = (manual_text or "").strip()
    if tr and tx and tr != tx:
        return f"{tr}\n\nComplemento informado pelo usuário:\n{tx}"
    return tx or tr


def _ui_css() -> None:
    st.markdown(
        """
<style>
  .bp-wrap { max-width: 1200px; margin: 0 auto; }
  .bp-hero {
    background: linear-gradient(135deg, #0f172a 0%, #14532d 50%, #166534 100%);
    color: #f8fafc;
    border-radius: 16px;
    padding: 1.25rem 1.5rem 1.35rem 1.5rem;
    margin-bottom: 1rem;
    box-shadow: 0 10px 30px rgba(15, 23, 42, 0.12);
  }
  .bp-hero h1 {
    font-size: 1.6rem;
    line-height: 1.2;
    margin: 0 0 0.35rem 0;
    font-weight: 700;
    letter-spacing: -0.02em;
  }
  .bp-hero p { margin: 0; opacity: 0.92; font-size: 0.95rem; }
  .bp-badges { display: flex; flex-wrap: wrap; gap: 0.4rem; margin-top: 0.6rem; }
  .bp-badge {
    display: inline-block;
    background: rgba(255,255,255,0.12);
    border: 1px solid rgba(255,255,255,0.18);
    padding: 0.2rem 0.55rem;
    border-radius: 999px;
    font-size: 0.78rem;
  }
  .bp-panel {
    background: #ffffff;
    border: 1px solid #e2e8f0;
    border-radius: 14px;
    padding: 1rem 1rem 0.5rem 1rem;
    margin-bottom: 0.75rem;
  }
  .bp-panel-title {
    font-size: 0.85rem;
    font-weight: 600;
    color: #0f172a;
    margin: 0 0 0.5rem 0;
    letter-spacing: 0.02em;
  }
  .bp-muted { color: #64748b; font-size: 0.9rem; }
  div.stButton > button[kind="primary"] {
    background: linear-gradient(90deg, #16a34a, #22c55e) !important;
    border: none !important;
    color: #fff !important;
    font-weight: 600 !important;
    border-radius: 10px !important;
    padding: 0.6rem 1.1rem !important;
  }
  div.stButton > button[kind="primary"]:hover { filter: brightness(1.04); }
  .block-container { padding-top: 1.5rem; padding-bottom: 2.5rem; }
</style>
        """,
        unsafe_allow_html=True,
    )


def _render_result_panel(
    transcription: str,
    input_text: str,
    sexo: str,
    rows: list[dict],
    structured_data: dict,
    db_path: Path | str,
) -> None:
    st.subheader("Resultado")
    tab_sum, tab_raw = st.tabs(["Resumo", "Detalhes & JSON"])

    with tab_sum:
        st.markdown(f"**Sexo** · `{sexo or 'Prefiro não informar'}`")
        if rows:
            for idx, r in enumerate(rows, start=1):
                c1, c2, c3, c4 = st.columns(4)
                c1.metric("Prova", r.get("Prova") or "—")
                c2.metric("Distância", r.get("Distância") or "—")
                c3.metric("Tempo", r.get("Tempo") or "—")
                c4.metric("Altimetria", r.get("Altimetria") or "—")
                if idx < len(rows):
                    st.divider()
        else:
            st.info("Nenhuma linha estruturada retornou desta extração.")

    with tab_raw:
        c1, c2 = st.columns(2, gap="large")
        with c1:
            st.markdown("**Texto usado pelo modelo**")
            st.text_area(
                "base_ia",
                value=input_text,
                height=200,
                label_visibility="collapsed",
            )
        with c2:
            if transcription:
                st.markdown("**Transcrição (áudio)**")
                st.text_area(
                    "transcricao",
                    value=transcription,
                    height=200,
                    label_visibility="collapsed",
                )
            else:
                st.caption("Sem gravação de áudio nesta extração (apenas texto).")
        st.markdown("**JSON completo (debug)**")
        st.json(structured_data)

    st.caption("Arquivo alvo: `results.xlsx` (append na planilha existente).")
    try:
        st.success(f"Salvo: `{Path(db_path).name}`", icon="✅")
    except TypeError:
        st.success(f"Salvo: `{Path(db_path).name}`")


def render_page() -> None:
    if st is None:
        raise RuntimeError("Pacote `streamlit` não está instalado.")

    st.set_page_config(page_title="Histórico de Atletas", page_icon="⛰️", layout="wide")
    _ui_css()

    for key, default in (
        ("last_transcription", ""),
        ("last_input_text", ""),
        ("last_sexo", "Prefiro não informar"),
        ("last_rows", []),
        ("last_structured", {}),
        ("last_db_path", ""),
    ):
        st.session_state.setdefault(key, default)

    api_ok = bool(os.getenv("OPENAI_API_KEY", "").strip())
    st.markdown(
        f"""
<div class="bp-hero">
  <h1>Histórico de Atletas</h1>
  <p>Transcreve o áudio, corrige nomes de franquias, busca distância e D+ na web e grava em <b>results.xlsx</b>.</p>
  <div class="bp-badges">
    <span class="bp-badge">{"API configurada" if api_ok else "Falta OPENAI_API_KEY"}</span>
    <span class="bp-badge">Whisper + modelo</span>
    <span class="bp-badge">Busca de elevação</span>
  </div>
</div>
        """,
        unsafe_allow_html=True,
    )

    col_in, col_out = st.columns([1, 1.05], gap="large")

    with col_in:
        st.markdown('<div class="bp-panel">', unsafe_allow_html=True)
        st.markdown('<p class="bp-panel-title">1 · Entrada</p>', unsafe_allow_html=True)
        st.caption("Preencha o sexo, grave ou descreva as provas e tempos.")

        gender = st.selectbox("Sexo", GENDER_OPTIONS, index=0)

        audio_source = None
        if hasattr(st, "audio_input"):
            audio_source = st.audio_input("Gravar áudio (microfone)", help="O áudio vira texto automaticamente.")
        elif hasattr(st, "experimental_audio_input"):
            audio_source = st.experimental_audio_input("Gravar áudio (microfone)")
        else:
            st.info("Microfone indisponível nesta versão do Streamlit. Use o campo de texto abaixo.")

        manual_text = st.text_area(
            "Corridas, distâncias e tempos",
            height=200,
            placeholder=(
                "Ex.: Fiz Indomit 35k em 5h. KTR Ilhabela 21k 2h20. La Mision 50k em Campos do Jordão..."
            ),
        )

        if audio_source is not None:
            try:
                audio_bytes = audio_source.getvalue() if hasattr(audio_source, "getvalue") else None
            except Exception:
                audio_bytes = None
            if audio_bytes:
                st.audio(audio_bytes, format="audio/wav")
        st.markdown("</div>", unsafe_allow_html=True)

        if not api_ok:
            st.warning("Defina a variável `OPENAI_API_KEY` para transcrever, extrair e buscar D+ na web.")

        has_input = audio_source is not None or bool(manual_text.strip())
        try:
            run = st.button(
                "Extrair e salvar no Excel",
                type="primary",
                use_container_width=True,
                disabled=not has_input,
            )
        except TypeError:
            run = st.button("Extrair e salvar no Excel", type="primary", disabled=not has_input)
        st.caption("Cada clique anexa uma ou mais linhas em `results.xlsx` (sem sobrescrever o histórico).")

    with col_out:
        if run and has_input:
            try:
                with st.spinner("Transcrevendo, extraindo, buscando distância e D+ na web…"):
                    result = extract_infos(
                        audio_source,
                        manual_text=manual_text,
                        reported_gender=gender,
                    )
                    structured_data = result.get("structured_data", {})
                    rows = enrich_rows_with_web(
                        structured_data.get("rows", []),
                        contexto_atleta=result.get("input_text", ""),
                    )
                    structured_data["rows"] = rows
                    db_path = save_results(
                        rows,
                        sexo=structured_data.get("sexo", "Prefiro não informar"),
                        input_text=result.get("input_text", ""),
                    )
            except Exception as exc:
                st.error(f"Não deu certo: {exc}")
            else:
                st.session_state["last_transcription"] = result.get("transcription", "")
                st.session_state["last_input_text"] = result.get("input_text", "")
                st.session_state["last_sexo"] = structured_data.get("sexo", "Prefiro não informar")
                st.session_state["last_rows"] = rows
                st.session_state["last_structured"] = structured_data
                st.session_state["last_db_path"] = str(db_path)
                try:
                    st.rerun()
                except Exception:
                    if hasattr(st, "experimental_rerun"):
                        st.experimental_rerun()

        if st.session_state.get("last_structured") or st.session_state.get("last_rows"):
            _render_result_panel(
                st.session_state.get("last_transcription", ""),
                st.session_state.get("last_input_text", ""),
                st.session_state.get("last_sexo", "Prefiro não informar"),
                st.session_state.get("last_rows", []),
                st.session_state.get("last_structured", {}),
                st.session_state.get("last_db_path", RESULTS_PATH),
            )
        else:
            st.markdown('<div class="bp-panel">', unsafe_allow_html=True)
            st.markdown('<p class="bp-panel-title">2 · Resultado</p>', unsafe_allow_html=True)
            st.markdown(
                '<p class="bp-muted">Aqui aparecem o resumo, a tabela e o JSON. '
                "Preencha a esquerda e clique em <b>Extrair e salvar no Excel</b>.</p>",
                unsafe_allow_html=True,
            )
            st.markdown("</div>", unsafe_allow_html=True)


if __name__ == "__main__":
    render_page()
