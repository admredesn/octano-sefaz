// ============================================================
// Portal Empresas · Postos SN (14/10/2026). JS puro, sem build.
// Servidor: empresas_portal.py (/empresas/api/*). Login: CNPJ + senha da empresa (1o acesso: codigo por WhatsApp para criar a senha; /empresas/api/cnpj/*) ou CPF + senha do app (/cashback/api/login).
// Regra: nada vindo do servidor entra no HTML sem esc(); nenhum dado vai dentro de onclick="…"
// (o bug do botão "Copiar" do app): os botões levam data-acao / data-id e há UM tratador de clique.
// ============================================================
"use strict";

const LS = {
  get(k) { try { return localStorage.getItem(k); } catch (e) { return null; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch (e) { /* navegação privada */ } },
  del(k) { try { localStorage.removeItem(k); } catch (e) { /* */ } },
};
const S = { token: LS.get("psn_emp_token") || "", eu: null, empresa: null, rota: "visao", cache: {}, consumo: { visao: "abast", pagina: 1 },
            faturas: { aba: "abertas", sel: null }, placas: { filtro: "todas", busca: "" } };
const raiz = () => document.getElementById("app");

// ---------------------------------------------------------------- utilidades
function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function num(v, d) { return Number(v || 0).toLocaleString("pt-BR", { minimumFractionDigits: d || 0, maximumFractionDigits: d || 0 }); }
function brl(v) { return "R$ " + num(v, 2); }
function dataBr(iso) { const s = String(iso || "").slice(0, 10); return s.length === 10 ? s.slice(8, 10) + "/" + s.slice(5, 7) + "/" + s.slice(0, 4) : ""; }
function horaBr(ts) {
  const d = new Date(ts);
  if (isNaN(d)) return "";
  const b = new Date(d.getTime() - 3 * 3600 * 1000);                     // Brasília (sem horário de verão desde 2019)
  const p = n => String(n).padStart(2, "0");
  return p(b.getUTCDate()) + "/" + p(b.getUTCMonth() + 1) + " " + p(b.getUTCHours()) + ":" + p(b.getUTCMinutes());
}
function placaFmt(p) { p = String(p || "").toUpperCase().replace(/[^A-Z0-9]/g, ""); return /^[A-Z]{3}\d{4}$/.test(p) ? p.slice(0, 3) + "-" + p.slice(3) : p; }
function chip(p, peq) {
  const f = placaFmt(p), antiga = /^[A-Z]{3}-\d{4}$/.test(f);
  return `<span class="placa${antiga ? " antiga" : ""}${peq ? " p" : ""}"><span class="br">BRASIL</span><span class="n">${esc(f)}</span></span>`;
}
function hojeIso() { const b = new Date(Date.now() - 3 * 3600 * 1000); return b.toISOString().slice(0, 10); }
function iniMesIso() { return hojeIso().slice(0, 8) + "01"; }

async function api(caminho, corpo, metodo) {
  let r;
  try {
    r = await fetch(caminho, {
      method: metodo || (corpo ? "POST" : "GET"),
      headers: { "Content-Type": "application/json", Authorization: "Bearer " + S.token },
      body: corpo ? JSON.stringify(corpo) : undefined,
    });
  } catch (e) { return { http: 0, erro: "Sem conexão com a internet. Tente de novo." }; }
  let j;
  try { j = await r.json(); } catch (e) { j = { erro: "Resposta inválida do servidor (" + r.status + ")." }; }
  if (r.status === 401 && S.token && /sess/i.test(j.erro || "")) { sair(true); }
  return Object.assign({ http: r.status }, j);
}
const Q = (obj) => Object.entries(obj).filter(([, v]) => v !== "" && v != null).map(([k, v]) => k + "=" + encodeURIComponent(v)).join("&");
const emp = () => S.empresa && S.empresa.id;
const eq = () => "empresa=" + encodeURIComponent(emp());

function aviso(txt, ms) {
  const t = document.createElement("div");
  t.className = "toast"; t.textContent = txt;
  document.body.appendChild(t);
  setTimeout(() => t.remove(), ms || 3200);
}
function janela(html, larga) {
  fechaJanela();
  const f = document.createElement("div");
  f.className = "fundo"; f.id = "janela";
  f.innerHTML = `<div class="janela${larga ? " larga" : ""}" role="dialog" aria-modal="true">${html}</div>`;
  f.addEventListener("click", e => { if (e.target === f) fechaJanela(); });
  document.body.appendChild(f);
}
function fechaJanela() { const f = document.getElementById("janela"); if (f) f.remove(); pixParar(); }

// copiar: o texto vai em data-copiar; mostra o aviso e o botão vira "✓ Copiado"
function copiarTexto(txt, aviso_, depois) {
  const ok = () => { aviso(aviso_ || "Copiado", 2400); if (depois) depois(); };
  const velho = () => {
    const t = document.createElement("textarea");
    t.value = txt; t.setAttribute("readonly", ""); t.style.cssText = "position:fixed;top:0;left:0;width:1px;height:1px;opacity:0;font-size:16px";
    document.body.appendChild(t);
    let feito = false;
    try { t.focus(); t.select(); t.setSelectionRange(0, txt.length); feito = document.execCommand("copy"); } catch (e) { feito = false; }
    t.remove();
    return feito;
  };
  try {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(txt).then(ok, () => { velho() ? ok() : aviso("Não consegui copiar. Selecione o código e copie.", 4000); });
      return;
    }
  } catch (e) { /* cai no método antigo */ }
  velho() ? ok() : aviso("Não consegui copiar. Selecione o código e copie.", 4000);
}

async function baixar(caminho, nomePadrao) {
  aviso("Preparando o arquivo…", 1500);
  let r;
  try { r = await fetch(caminho, { headers: { Authorization: "Bearer " + S.token } }); } catch (e) { aviso("Sem conexão. Tente de novo."); return; }
  if (!r.ok) { let m = "Não consegui gerar o arquivo agora."; try { m = (await r.json()).erro || m; } catch (e) { /* */ } aviso(m, 4500); return; }
  const cd = r.headers.get("Content-Disposition") || "";
  const m = /filename="?([^";]+)"?/.exec(cd);
  const blob = await r.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url; a.download = (m && m[1]) || nomePadrao || "arquivo";
  document.body.appendChild(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 5000);
}

// ---------------------------------------------------------------- entrar / sair
function telaEntrar(msg) {
  raiz().innerHTML = `<div class="entrar"><form class="caixa" id="f-entrar" autocomplete="on">
    <div class="marca" style="color:var(--azul)"><div class="logo" style="background:var(--azul)"><b style="color:#fff">SN</b><i></i></div>
      <div><div class="nome" style="color:var(--azul)">Postos SN</div><div class="sub">EMPRESAS</div></div></div>
    <div><h1 style="font-size:28px" id="e-titulo">Entrar</h1><div class="mut" id="e-dica" style="margin-top:4px">Informe o CNPJ da empresa e a senha.</div></div>
    ${msg ? `<div class="erro" role="alert">${esc(msg)}</div>` : ""}
    <label class="campo">CNPJ da empresa <span class="mut">(ou CPF do app)</span><input class="entrada" id="e-doc" inputmode="numeric" autocomplete="username" placeholder="00.000.000/0000-00" maxlength="18"></label>
    <label class="campo" id="b-senha">Senha<input class="entrada" id="e-senha" type="password" autocomplete="current-password"></label>
    <label class="campo" id="b-codigo" style="display:none">Código recebido por WhatsApp<input class="entrada" id="e-codigo" inputmode="numeric" autocomplete="one-time-code" placeholder="000000" maxlength="6"></label>
    <label class="campo" id="b-nova" style="display:none">Crie a senha da empresa <span class="mut">(mínimo 6 caracteres)</span><input class="entrada" id="e-nova" type="password" autocomplete="new-password"></label>
    <label class="campo" id="b-nova2" style="display:none">Repita a senha<input class="entrada" id="e-nova2" type="password" autocomplete="new-password"></label>
    <div class="mut" id="e-aviso" style="font-size:13.5px;line-height:19px;display:none"></div>
    <button class="btn bloco" type="submit" id="e-ok">Entrar</button>
    <button class="btn bloco" type="button" id="e-primeiro" style="background:transparent;color:var(--azul);border:1px solid var(--azul)">Primeiro acesso ou esqueci a senha</button>
    <button class="btn bloco" type="button" id="e-voltar" style="display:none;background:transparent;color:var(--azul);border:1px solid var(--azul)">Voltar</button>
    <div class="mut" style="font-size:13.5px;line-height:19px">O acesso é liberado pelo posto para o financeiro da empresa. No primeiro acesso, o código chega por
      WhatsApp para o financeiro cadastrado e a empresa cria a sua senha. Quem já usa o aplicativo Postos SN também entra com o <b>CPF</b> e a senha do aplicativo.</div>
  </form></div>`;
  const $ = id => document.getElementById(id);
  const doc = $("e-doc"), ok = $("e-ok");
  let passo = "login";                 // login | codigo (primeiro acesso / esqueci) | so-codigo (servidor sem senha habilitada)
  const dig = () => doc.value.replace(/\D/g, "");
  const ehCpf = () => dig().length === 11;
  const mostra = (id, v) => { $(id).style.display = v ? "" : "none"; };
  const tela = () => {
    const cnpjMode = dig().length > 11, rec = passo === "codigo" || passo === "so-codigo";
    mostra("b-senha", !rec);
    mostra("b-codigo", rec);
    mostra("b-nova", passo === "codigo"); mostra("b-nova2", passo === "codigo");
    mostra("e-primeiro", !rec && cnpjMode); mostra("e-voltar", rec);
    mostra("e-aviso", rec);
    $("e-titulo").textContent = passo === "codigo" ? "Criar a senha" : (passo === "so-codigo" ? "Entrar com o código" : "Entrar");
    $("e-dica").textContent = rec ? "Digite o código que chegou por WhatsApp" + (passo === "codigo" ? " e crie a senha da empresa." : ".")
      : (ehCpf() ? "Use a mesma senha do aplicativo." : "Informe o CNPJ da empresa e a senha.");
    ok.textContent = passo === "codigo" ? "Salvar senha e entrar" : "Entrar";
  };
  doc.addEventListener("input", () => {
    const d = doc.value.replace(/\D/g, "").slice(0, 14);
    if (d.length <= 11) doc.value = d.replace(/(\d{3})(\d)/, "$1.$2").replace(/(\d{3})(\d)/, "$1.$2").replace(/(\d{3})(\d{1,2})$/, "$1-$2");
    else doc.value = d.replace(/^(\d{2})(\d)/, "$1.$2").replace(/^(\d{2})\.(\d{3})(\d)/, "$1.$2.$3").replace(/\.(\d{3})(\d)/, ".$1/$2").replace(/(\d{4})(\d)/, "$1-$2");
    if (passo !== "login") passo = "login";
    tela();
  });
  const pedirCodigo = async (modo) => {
    if (dig().length !== 14) { aviso("Informe o CNPJ da empresa (14 números).", 3600); return; }
    ok.disabled = true; const t0 = ok.textContent; ok.textContent = "Enviando…";
    const r = await api("/empresas/api/cnpj/pedir", { cnpj: dig() });
    ok.disabled = false; ok.textContent = t0;
    if (!r.ok) { telaEntrar(r.erro || "Não consegui pedir o código."); return; }
    passo = modo;
    tela();
    $("e-aviso").textContent = r.aviso || "";
    $("e-codigo").focus();
  };
  $("e-primeiro").addEventListener("click", () => pedirCodigo("codigo"));
  $("e-voltar").addEventListener("click", () => { passo = "login"; tela(); });
  $("f-entrar").addEventListener("submit", async e => {
    e.preventDefault();
    const d = dig();
    if (d.length !== 11 && d.length !== 14) { aviso("Informe o CNPJ da empresa (14 números) ou o CPF (11 números).", 3600); return; }
    const entrou = async (r) => {
      if (r.token) { S.token = r.token; LS.set("psn_emp_token", r.token); await iniciar(); return true; }
      return false;
    };
    ok.disabled = true; const rotulo = ok.textContent; ok.textContent = "Entrando…";
    if (d.length === 11) {                                   // CPF + senha do aplicativo
      const r = await api("/cashback/api/login", { cpf: d, senha: $("e-senha").value });
      if (await entrou(r)) return;
      telaEntrar(r.verificar ? "Confirme a sua conta primeiro: entre uma vez no aplicativo Postos SN e informe o código recebido." : (r.erro || "Não consegui entrar."));
      return;
    }
    if (passo === "login") {                                 // CNPJ + senha
      if (!$("e-senha").value) { ok.disabled = false; ok.textContent = rotulo; aviso("Informe a senha da empresa.", 3200); return; }
      const r = await api("/empresas/api/cnpj/login", { cnpj: d, senha: $("e-senha").value });
      if (await entrou(r)) return;
      if (r.codigo_apenas) { ok.disabled = false; ok.textContent = rotulo; await pedirCodigo("so-codigo"); return; }
      telaEntrar(r.erro || "Não consegui entrar.");
      return;
    }
    const codigo = $("e-codigo").value.replace(/\D/g, "");
    if (codigo.length !== 6) { ok.disabled = false; ok.textContent = rotulo; aviso("Digite o código de 6 números.", 3200); return; }
    if (passo === "so-codigo") {                             // servidor sem a tabela da senha: entra so' com o codigo
      const r = await api("/empresas/api/cnpj/entrar", { cnpj: d, codigo });
      if (await entrou(r)) return;
      ok.disabled = false; ok.textContent = rotulo; aviso(r.erro || "Código incorreto ou vencido. Peça um novo.", 4200); return;
    }
    const n1 = $("e-nova").value, n2 = $("e-nova2").value;   // passo === "codigo": cadastra a senha
    if (n1.length < 6) { ok.disabled = false; ok.textContent = rotulo; aviso("A senha precisa ter pelo menos 6 caracteres.", 3600); return; }
    if (n1 !== n2) { ok.disabled = false; ok.textContent = rotulo; aviso("As senhas não conferem.", 3600); return; }
    const r = await api("/empresas/api/cnpj/senha", { cnpj: d, codigo, senha: n1 });
    if (await entrou(r)) return;
    ok.disabled = false; ok.textContent = rotulo;
    aviso(r.erro || "Código incorreto ou vencido. Peça um novo.", 4200);
  });
  tela();
}
function sair(silencioso) {
  S.token = ""; S.eu = null; S.empresa = null; S.cache = {};
  LS.del("psn_emp_token");
  telaEntrar(silencioso ? "Sua sessão expirou. Entre de novo." : "");
}

async function iniciar() {
  if (!S.token) { telaEntrar(); return; }
  raiz().innerHTML = `<div class="entrar"><div class="caixa"><div class="esq" style="height:28px;width:60%"></div><div class="esq" style="height:44px"></div></div></div>`;
  const r = await api("/empresas/api/eu");
  if (r.http === 401) { telaEntrar(); return; }
  if (!r.ok) { S.token = ""; LS.del("psn_emp_token"); telaEntrar(r.erro || "Sem acesso ao Portal Empresas."); return; }
  S.eu = r;
  const guardada = LS.get("psn_emp_empresa");
  S.empresa = r.empresas.find(e => e.id === guardada) || r.empresas[0];
  rotear();
}

// ---------------------------------------------------------------- moldura e rotas
const ABAS = [["visao", "Visão geral"], ["placas", "Placas"], ["consumo", "Consumo"], ["faturas", "Faturas e boletos"]];
function moldura(titulo, sub, acoesHtml, corpoHtml) {
  const emps = S.eu.empresas;
  const trocar = emps.length > 1
    ? `<label class="campo" style="margin-top:6px">Empresa<select class="entrada" id="sel-emp">${emps.map(e => `<option value="${esc(e.id)}"${e.id === emp() ? " selected" : ""}>${esc(e.nome)} · ${esc(e.posto)}</option>`).join("")}</select></label>` : "";
  return `<div class="moldura">
    <aside class="lateral">
      <div class="marca"><div class="logo"><b>SN</b><i></i></div><div><div class="nome">Postos SN</div><div class="sub">EMPRESAS</div></div></div>
      <nav class="menu">${ABAS.map(([k, r]) => `<a href="#/${k}" class="${S.rota === k ? "ativo" : ""}">${esc(r)}</a>`).join("")}</nav>
      <div class="usuario"><div class="quem">Entrou como<br><b>${esc(S.eu.usuario)}</b><br>Financeiro</div><button data-acao="sair">Sair</button></div>
    </aside>
    <main class="conteudo">
      <nav class="abas-cel" aria-label="Seções">${ABAS.map(([k, r]) => `<a href="#/${k}" class="${S.rota === k ? "ativo" : ""}">${esc(r)}</a>`).join("")}</nav>
      <header class="topo"><div style="min-width:0"><h1>${esc(titulo)}</h1><div class="sub">${esc(sub)}</div>${trocar}</div>
        <div class="acoes">${acoesHtml || ""}</div></header>
      ${corpoHtml}
    </main></div>`;
}
function esqueleto() {
  return `<div class="esq" style="height:110px"></div><div class="duas"><div class="esq grande" style="height:340px"></div><div class="esq peq" style="height:340px"></div></div><div class="esq" style="height:300px"></div>`;
}
function rotear() {
  const h = (location.hash || "").replace(/^#\/?/, "") || "visao";
  S.rota = ABAS.some(a => a[0] === h) ? h : "visao";
  const f = { visao: telaVisao, placas: telaPlacas, consumo: telaConsumo, faturas: telaFaturas }[S.rota];
  f();
  window.scrollTo(0, 0);
}
window.addEventListener("hashchange", () => { if (S.token && S.eu) rotear(); });

function chaveBarra() { /* reservado */ }

// ---------------------------------------------------------------- VISÃO GERAL
async function telaVisao() {
  raiz().innerHTML = moldura(S.empresa.nome, `CNPJ ${S.empresa.documento ? S.empresa.documento.replace(/^(\d{2})(\d{3})(\d{3})(\d{4})(\d{2})$/, "$1.$2.$3/$4-$5") : ""} · atendida pelo ${S.empresa.posto}`,
    `<a class="btn" href="#/placas" data-acao="nova-placa-rota">+ Cadastrar placa</a>`, esqueleto());
  const r = await api("/empresas/api/visao?" + eq());
  if (S.rota !== "visao") return;
  if (!r.ok) { raiz().innerHTML = moldura(S.empresa.nome, "", "", `<div class="aviso">${esc(r.erro || "Não consegui carregar.")}</div>`); return; }
  S.cache.visao = r;
  desenhaVisao();
}
function desenhaVisao() {
  const r = S.cache.visao, k = r.kpi;
  const per = S.visPer || "mes", met = S.visMet || "litros";
  const usado = k.limite > 0 ? Math.min(100, Math.round(k.limite_usado / k.limite * 100)) : null;
  const rows = (r.grafico[per] || []).slice().sort((a, b) => (met === "litros" ? b.litros - a.litros : b.valor - a.valor));
  const mx = rows.length ? (met === "litros" ? rows[0].litros : rows[0].valor) || 1 : 1;
  const tot = r.totais[per] || {};
  const nomePer = { mes: "Este mês", d30: "Últimos 30 dias", d90: "Últimos 90 dias" }[per];
  const mediaN = Math.max(1, k.placas_ativas);
  const corpo = `
    <section class="cartao resumo" aria-label="Resumo">
      <div><span class="r">CONSUMO NO MÊS</span><span class="v">${brl(k.consumo_mes_valor)}</span><span class="s">${num(k.consumo_mes_litros)} litros</span></div>
      <div><span class="r">FATURAS EM ABERTO</span><span class="v">${brl(k.abertas_valor)}</span>
        <span class="s ${k.vencidas_qtd ? "vm" : ""}">${k.vencidas_qtd ? `${k.vencidas_qtd} vencida${k.vencidas_qtd > 1 ? "s" : ""} · ${brl(k.vencidas_valor)}` : `${k.abertas_qtd} em aberto`}</span></div>
      <div><span class="r">LIMITE A PRAZO</span><span class="v">${usado == null ? "—" : usado + "%"}</span>
        ${usado == null ? "" : `<div class="barra"><i style="width:${usado}%"></i></div>`}
        <span class="s">${k.limite > 0 ? `${brl(k.limite_usado)} de ${brl(k.limite)}` : "Sem limite definido"}</span></div>
      <div><span class="r">PLACAS ATIVAS</span><span class="v">${k.placas_ativas}</span><span class="s">${k.placas_desativadas} desativadas · ${k.placas_bloqueadas} bloqueada${k.placas_bloqueadas === 1 ? "" : "s"} pelo posto</span></div>
    </section>
    <div class="duas">
      <section class="cartao pad grande" id="consumo">
        <div style="display:flex;flex-wrap:wrap;gap:12px;align-items:center;justify-content:space-between">
          <div><h2>Consumo por placa</h2><div class="leg">${nomePer} · as 8 placas que mais abasteceram · <a href="#/consumo">ver cada abastecimento</a></div></div>
          <div class="acoes">
            <div class="seg"><button data-acao="vis-met" data-v="litros" class="${met === "litros" ? "on" : ""}">Litros</button><button data-acao="vis-met" data-v="valor" class="${met === "valor" ? "on" : ""}">R$</button></div>
            <div class="seg"><button data-acao="vis-per" data-v="mes" class="${per === "mes" ? "on" : ""}">Mês</button><button data-acao="vis-per" data-v="d30" class="${per === "d30" ? "on" : ""}">30 dias</button><button data-acao="vis-per" data-v="d90" class="${per === "d90" ? "on" : ""}">90 dias</button></div>
          </div>
        </div>
        <div class="grafico" style="margin-top:18px">
          ${rows.length ? rows.map(x => `<div class="g-lin"><div>${chip(x.placa)}</div>
            <div><div class="tr"><i style="width:${Math.max(3, Math.round((met === "litros" ? x.litros : x.valor) / mx * 100))}%"></i></div><div class="vei">${esc(x.veiculo)}</div></div>
            <div class="vv">${met === "litros" ? num(x.litros) + " L" : brl(x.valor)}<small>${met === "litros" ? brl(x.valor) : num(x.litros) + " L"}</small></div></div>`).join("")
            : `<div class="vazio">Nenhum abastecimento neste período.</div>`}
        </div>
        <div style="display:flex;flex-wrap:wrap;justify-content:space-between;gap:8px;border-top:1px solid var(--borda);margin-top:16px;padding-top:14px;color:var(--mut)">
          <span>Total da frota: <b style="color:var(--azul)">${met === "litros" ? num(tot.litros) + " L" : brl(tot.valor)}</b></span>
          <span>Média por placa ativa: <b style="color:var(--azul)">${met === "litros" ? num((tot.litros || 0) / mediaN) + " L" : brl((tot.valor || 0) / mediaN)}</b></span>
        </div>
      </section>
      <section class="cartao pad peq" id="faturas">
        <div style="display:flex;justify-content:space-between;align-items:baseline;gap:10px"><h2>Faturas em aberto</h2><a href="#/faturas" style="font-weight:600;text-decoration:none">Ver todas</a></div>
        ${r.faturas_abertas.length ? r.faturas_abertas.map(f => cartaoFatura(f)).join("") : `<div class="vazio">Nenhuma fatura em aberto.</div>`}
        ${k.nao_faturado > 0 ? `<div class="nota" style="margin-top:6px"><b style="color:var(--azul)">${brl(k.nao_faturado)}</b> em abastecimentos ainda não faturados. Entram na próxima fatura.</div>` : ""}
      </section>
    </div>
    <section class="cartao" style="padding:22px 0 8px">
      <div style="padding:0 24px 16px;display:flex;flex-wrap:wrap;gap:12px;align-items:center;justify-content:space-between">
        <div><h2>Placas cadastradas</h2><div class="leg">Só as placas desta lista abastecem a prazo para a empresa. Desativar não apaga o histórico.</div></div>
        <a class="btn ghost" href="#/placas">Gerenciar placas</a>
      </div>
      ${tabelaPlacas(r.placas.slice(0, 8), false)}
      ${r.placas.length > 8 ? `<div style="padding:12px 24px"><a href="#/placas" style="font-weight:600;text-decoration:none">Ver as ${r.placas.length} placas</a></div>` : ""}
    </section>
    ${r.faturas_pagas.length ? `<section class="cartao pad"><h2 style="margin-bottom:8px">Faturas pagas</h2>
      ${r.faturas_pagas.map(f => `<div style="display:flex;flex-wrap:wrap;gap:10px 18px;align-items:center;justify-content:space-between;padding:12px 0;border-top:1px solid var(--linha)">
        <div style="min-width:160px"><div class="val" style="font-size:16px">Fatura ${esc(f.numero)}</div><div class="mut" style="font-size:13.5px">Paga em ${dataBr(f.paga_em)}${f.forma ? " por " + esc(f.forma) : ""}</div></div>
        <div class="val">${brl(f.valor)}</div>
        <button class="btn ghost peq" data-acao="fat-pdf" data-id="${esc(f.id)}">Baixar PDF da fatura</button></div>`).join("")}</section>` : ""}`;
  raiz().innerHTML = moldura(S.empresa.nome, `CNPJ ${S.empresa.documento ? S.empresa.documento.replace(/^(\d{2})(\d{3})(\d{3})(\d{4})(\d{2})$/, "$1.$2.$3/$4-$5") : ""} · atendida pelo ${S.empresa.posto}`,
    `<button class="btn" data-acao="nova-placa">+ Cadastrar placa</button>`, corpo);
}

function cartaoFatura(f) {
  const venc = f.vencida
    ? `<span class="chip verm">Vencida há ${f.dias_atraso} dia${f.dias_atraso > 1 ? "s" : ""}</span>`
    : (f.dias_para_vencer != null ? `<span class="chip ouro">${f.dias_para_vencer === 0 ? "Vence hoje" : "Vence em " + f.dias_para_vencer + " dia" + (f.dias_para_vencer > 1 ? "s" : "")}</span>` : "");
  const enc = f.vencida && (f.multa || f.juros) ? `<div class="mut" style="font-size:13px">Com multa ${brl(f.multa)} e juros ${brl(f.juros)}: <b style="color:var(--azul)">${brl(f.valor_atualizado)}</b></div>` : "";
  const bol = f.boleto.estado;
  const rotBol = bol === "disponivel" ? "Boleto" : (bol === "gerando" ? "Gerando boleto…" : (bol === "vencido" ? "Gerar novo boleto" : "Gerar boleto"));
  return `<div class="fat">
    <div class="l1"><div><div class="n">Fatura ${esc(f.numero)}${f.parcela ? " · parcela " + esc(f.parcela) : ""}</div>
      <div class="mut" style="font-size:13.5px">Emitida em ${dataBr(f.emissao)} · vence em ${dataBr(f.vencimento)}</div></div>
      <div class="vl">${brl(f.valor_atualizado || f.valor)}</div></div>
    <div>${venc}</div>${enc}
    <div class="ac">
      ${f.pix_ligado ? `<button class="btn peq" data-acao="fat-pix" data-id="${esc(f.id)}">Pagar com Pix</button>` : ""}
      <button class="btn ghost peq" data-acao="fat-boleto" data-id="${esc(f.id)}">${esc(rotBol)}</button>
      <a class="btn ghost peq" href="#/faturas" data-acao="fat-detalhe" data-id="${esc(f.id)}" style="border:0">Ver compras</a>
    </div></div>`;
}

// ---------------------------------------------------------------- PLACAS
function sitChip(p) {
  if (p.situacao === "bloqueada_posto") return `<span class="chip verm">Bloqueada pelo posto</span>`;
  if (p.situacao === "desativada") return `<span class="mut" style="font-weight:600;font-size:13px">Desativada</span>`;
  return `<span style="font-weight:600;font-size:13px;color:var(--verde)">Ativa</span>`;
}
function chavePlaca(p) {
  if (p.situacao === "bloqueada_posto") return "";
  return `<button class="chave${p.situacao === "ativa" ? " on" : ""}" data-acao="placa-chave" data-id="${esc(p.id)}" data-v="${p.situacao === "ativa" ? "0" : "1"}"
    aria-label="${p.situacao === "ativa" ? "Desativar" : "Ativar"} a placa ${esc(placaFmt(p.placa))}"><i></i></button>`;
}
function tabelaPlacas(lista, completa) {
  if (!lista.length) return `<div class="vazio">Nenhuma placa nesta lista.</div>`;
  const linha = p => `<tr style="${p.situacao === "desativada" ? "opacity:.55" : ""}">
    <td>${chip(p.placa)}</td><td>${esc(p.veiculo)}</td><td class="mut">${esc(p.motorista || "—")}</td>
    ${completa ? `<td class="mut">${esc(dataBr(p.cadastrada_em) || "")}</td>` : ""}
    <td class="d val" style="font-size:16px">${p.litros_mes != null ? (p.litros_mes > 0 ? num(p.litros_mes) + " L" : "—") : "—"}</td>
    <td><div style="display:flex;align-items:center;gap:10px">${chavePlaca(p)}${sitChip(p)}</div>
      ${p.situacao === "desativada" && p.desativada_por ? `<div class="mut" style="font-size:12.5px">Desativada em ${dataBr(p.desativada_em)} por ${esc(p.desativada_por)}</div>` : ""}
      ${p.situacao === "bloqueada_posto" ? `<div class="mut" style="font-size:12.5px">${esc(p.motivo || "Só o posto libera.")}</div>` : ""}</td>
    ${completa ? `<td style="text-align:right"><button class="btn ghost peq" data-acao="placa-editar" data-id="${esc(p.id)}">Editar</button></td>` : ""}</tr>`;
  return `<div class="rolar so-pc"><table class="t" style="min-width:820px"><thead><tr><th>PLACA</th><th>VEÍCULO</th><th>MOTORISTA</th>${completa ? "<th>CADASTRADA EM</th>" : ""}
      <th class="d" style="text-align:right">LITROS NO MÊS</th><th>SITUAÇÃO</th>${completa ? "<th></th>" : ""}</tr></thead><tbody>${lista.map(linha).join("")}</tbody></table></div>
    <div class="so-cel" style="padding:0 16px 12px;display:flex;flex-direction:column;gap:10px">${lista.map(p => `<div class="cart" style="${p.situacao === "desativada" ? "opacity:.6" : ""}">
      <div class="l">${chip(p.placa)}<div style="display:flex;align-items:center;gap:8px">${sitChip(p)}${chavePlaca(p)}</div></div>
      <div style="font-weight:500">${esc(p.veiculo)}</div>
      <div class="mut" style="font-size:13px">${esc(p.motorista || "Sem motorista fixo")}${p.litros_mes > 0 ? " · " + num(p.litros_mes) + " L no mês" : ""}</div>
      ${completa ? `<button class="btn ghost peq" style="align-self:flex-start" data-acao="placa-editar" data-id="${esc(p.id)}">Editar</button>` : ""}</div>`).join("")}</div>`;
}
async function telaPlacas() {
  raiz().innerHTML = moldura("Placas", `Cadastre, ative e desative as placas da ${S.empresa.nome}.`,
    `<button class="btn ghost" data-acao="importar-placas">Importar lista</button><button class="btn" data-acao="nova-placa">+ Cadastrar placa</button>`, esqueleto());
  const r = await api("/empresas/api/placas?" + eq());
  if (S.rota !== "placas") return;
  if (!r.ok) { raiz().innerHTML = moldura("Placas", "", "", `<div class="aviso">${esc(r.erro || "Não consegui carregar as placas.")}</div>`); return; }
  S.cache.placas = r.placas;
  desenhaPlacas();
}
function desenhaPlacas() {
  const todas = S.cache.placas || [], f = S.placas.filtro, b = S.placas.busca.trim().toLowerCase();
  const cont = { todas: todas.length, ativas: todas.filter(p => p.situacao === "ativa").length, desativadas: todas.filter(p => p.situacao === "desativada").length,
                 bloqueadas: todas.filter(p => p.situacao === "bloqueada_posto").length };
  let lista = todas.filter(p => f === "todas" ? true : (f === "ativas" ? p.situacao === "ativa" : (f === "desativadas" ? p.situacao === "desativada" : p.situacao === "bloqueada_posto")));
  if (b) lista = lista.filter(p => (p.placa + " " + p.veiculo + " " + p.motorista).toLowerCase().includes(b.replace(/[^a-z0-9 ]/g, "")));
  raiz().innerHTML = moldura("Placas", `Cadastre, ative e desative as placas da ${S.empresa.nome}.`,
    `<button class="btn ghost" data-acao="importar-placas">Importar lista</button><button class="btn" data-acao="nova-placa">+ Cadastrar placa</button>`, `
    <div class="aviso"><span aria-hidden="true">ⓘ</span><span>Só as placas desta lista abastecem a prazo para ${esc(S.empresa.nome)}. Placa que não estiver cadastrada é recusada no caixa de qualquer posto da rede.</span></div>
    <section class="cartao" style="padding:18px 0 0">
      <div style="padding:0 20px 14px;display:flex;flex-wrap:wrap;gap:12px;align-items:center;justify-content:space-between">
        <div class="seg">${[["todas", "Todas"], ["ativas", "Ativas"], ["desativadas", "Desativadas"], ["bloqueadas", "Bloqueadas pelo posto"]]
          .map(([k, r]) => `<button data-acao="placas-filtro" data-v="${k}" class="${f === k ? "on" : ""}">${r} (${cont[k]})</button>`).join("")}</div>
        <input class="entrada" id="placas-busca" style="max-width:300px" placeholder="Buscar placa, veículo ou motorista" value="${esc(S.placas.busca)}" aria-label="Buscar placa, veículo ou motorista">
      </div>
      ${tabelaPlacas(lista, true)}
      <div class="mut" style="padding:14px 20px">Quem desativa e quando fica registrado. Desativar uma placa não apaga o histórico de abastecimentos nem as faturas.</div>
    </section>`);
  const bi = document.getElementById("placas-busca");
  if (bi) bi.addEventListener("input", () => { S.placas.busca = bi.value; const pos = bi.selectionStart; desenhaPlacas(); const n = document.getElementById("placas-busca"); n.focus(); n.setSelectionRange(pos, pos); });
}

async function modalPlaca(veiculoId) {
  const cr = await api("/empresas/api/colaboradores?" + eq());
  const cols = cr.colaboradores || [];
  const v = veiculoId ? (S.cache.placas || []).find(p => p.id === veiculoId) : null;
  janela(`<div class="cab"><div><h2>${v ? "Editar placa" : "Cadastrar placa"}</h2>
      <div class="mut" style="margin-top:4px">${v ? "Veículo e motorista habitual." : "A placa já pode abastecer a prazo assim que for salva."}</div></div>
      <button class="btn ghost peq" data-acao="fecha">Fechar</button></div>
    <div class="grade-form">
      <label class="campo">Placa<input class="entrada" id="pl-placa" maxlength="8" value="${esc(v ? placaFmt(v.placa) : "")}" ${v ? "disabled" : ""}
        style="font:700 22px var(--titulo);letter-spacing:.1em;text-transform:uppercase" placeholder="AAA0A00" autocomplete="off"></label>
      <div class="mut" id="pl-dica" style="font-size:13px;margin-top:-8px">Aceita Mercosul (AAA0A00) e o modelo antigo (AAA-0000).</div>
      <label class="campo">Veículo<input class="entrada" id="pl-veic" maxlength="60" value="${esc(v ? v.veiculo : "")}" placeholder="Modelo ou apelido"></label>
      <label class="campo">Motorista habitual (opcional)<select class="entrada" id="pl-mot"><option value="">Sem motorista fixo</option>
        ${cols.map(c => `<option value="${esc(c.id)}"${v && v.motorista_id === c.id ? " selected" : ""}>${esc(c.nome)}</option>`).join("")}</select></label>
      <div class="erro" id="pl-erro" hidden></div>
    </div>
    <div style="display:grid;grid-template-columns:1fr 1.5fr;gap:10px"><button class="btn ghost" data-acao="fecha">Cancelar</button>
      <button class="btn" id="pl-ok" data-acao="placa-salvar" data-id="${esc(veiculoId || "")}">${v ? "Salvar" : "Cadastrar placa"}</button></div>`);
  const inp = document.getElementById("pl-placa");
  if (inp && !v) { inp.focus(); inp.addEventListener("input", () => { inp.value = inp.value.toUpperCase().replace(/[^A-Z0-9-]/g, ""); }); }
}
async function salvarPlaca(vid, btn) {
  const erro = document.getElementById("pl-erro");
  erro.hidden = true;
  btn.disabled = true;
  const corpo = { empresa: emp(), veiculo: document.getElementById("pl-veic").value.trim(), motorista_id: document.getElementById("pl-mot").value || null };
  let r;
  if (vid) r = await api("/empresas/api/placas/" + encodeURIComponent(vid), corpo);
  else { corpo.placa = document.getElementById("pl-placa").value; r = await api("/empresas/api/placas", corpo); }
  btn.disabled = false;
  if (!r.ok) { erro.textContent = r.erro || "Não consegui salvar."; erro.hidden = false; return; }
  fechaJanela();
  aviso(vid ? "Placa atualizada." : (r.resultado === "nova" ? "Placa cadastrada." : (r.resultado === "reativada" ? "Placa reativada." : "Esta placa já estava cadastrada.")));
  S.cache.visao = null;
  if (S.rota === "placas") telaPlacas(); else if (S.rota === "visao") telaVisao();
}
function modalImportar() {
  janela(`<div class="cab"><div><h2>Importar lista de placas</h2><div class="mut" style="margin-top:4px">Uma placa por linha. Se quiser, acrescente o veículo e o motorista separados por ponto e vírgula.</div></div>
      <button class="btn ghost peq" data-acao="fecha">Fechar</button></div>
    <label class="campo">Lista de placas<textarea class="entrada" id="imp-txt" rows="6" style="font-family:ui-monospace,Menlo,Consolas,monospace" placeholder="RTA4F21;Scania R450;Edson Ribeiro&#10;QUX7B09;Volvo FH 460"></textarea></label>
    <div id="imp-res"></div>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px"><button class="btn ghost" data-acao="imp-conferir">Conferir a lista</button>
      <button class="btn" data-acao="imp-gravar" id="imp-gravar" disabled>Cadastrar</button></div>`, true);
}
async function conferirImport(aplicar) {
  const txt = document.getElementById("imp-txt").value;
  if (!txt.trim()) { aviso("Cole a lista de placas primeiro."); return; }
  const r = await api("/empresas/api/placas/importar", { empresa: emp(), texto: txt, aplicar: !!aplicar });
  if (!r.ok) { aviso(r.erro || "Não consegui conferir a lista."); return; }
  const rot = { nova: "Nova", existente: "Já cadastrada", reativar: "Será reativada", reativada: "Reativada" };
  document.getElementById("imp-res").innerHTML = `<div class="lista-imp">${r.itens.map(i => `<div>
      <span style="font:700 16px var(--titulo);letter-spacing:.06em;width:92px">${esc(placaFmt(i.placa) || i.linha)}</span>
      <span style="flex:1;min-width:0;color:${i.ok ? "var(--txt)" : "var(--perigo)"}">${esc(i.ok ? [i.veiculo, i.motorista].filter(Boolean).join(" · ") || "—" : i.erro)}</span>
      <span class="chip ${i.ok ? "verde" : "verm"}">${esc(i.ok ? (rot[i.resultado || i.situacao] || "Ok") : "Corrigir")}</span></div>`).join("")}</div>
    <div class="mut" style="margin-top:8px">${r.resumo.validas} reconhecida${r.resumo.validas === 1 ? "" : "s"} · ${r.resumo.erros} com erro (ficam de fora)</div>`;
  const g = document.getElementById("imp-gravar");
  g.disabled = !r.resumo.validas || !!aplicar;
  g.textContent = r.resumo.validas ? `Cadastrar ${r.resumo.validas} placa${r.resumo.validas === 1 ? "" : "s"}` : "Cadastrar";
  if (aplicar) { aviso("Placas gravadas."); S.cache.visao = null; telaPlacas(); }
}

// ---------------------------------------------------------------- CONSUMO
async function telaConsumo() {
  const c = S.consumo;
  c.de = c.de || iniMesIso(); c.ate = c.ate || hojeIso();
  raiz().innerHTML = moldura("Consumo", "Cada abastecimento da frota, com o cupom assinado e a fatura em que entrou.", "", esqueleto());
  const r = await api("/empresas/api/consumo?" + Q({ empresa: emp(), de: c.de, ate: c.ate, placa: c.placa, produto: c.produto, fatura: c.fatura, pagina: c.pagina, tam: 50 }));
  if (S.rota !== "consumo") return;
  if (!r.ok) { raiz().innerHTML = moldura("Consumo", "", "", `<div class="aviso">${esc(r.erro || "Não consegui carregar o consumo.")}</div>`); return; }
  S.cache.consumo = r;
  desenhaConsumo();
}
function filtroConsumoQ(extra) { const c = S.consumo; return Q(Object.assign({ empresa: emp(), de: c.de, ate: c.ate, placa: c.placa, produto: c.produto, fatura: c.fatura }, extra || {})); }
function desenhaConsumo() {
  const r = S.cache.consumo, c = S.consumo, t = r.totais;
  const lin = r.linhas.map(l => `<tr>
    <td style="white-space:nowrap">${esc(horaBr(l.quando))}</td><td>${l.placa ? chip(l.placa, true) : "—"}</td><td class="mut">${esc(l.produto)}</td>
    <td class="d">${l.litros != null ? num(l.litros, 1) : ""}</td><td class="d mut">${l.preco != null ? num(l.preco, 2) : ""}</td>
    <td class="d val" style="font-size:15px">${brl(l.valor)}</td><td class="d mut">${l.km ? num(l.km) : ""}</td><td class="mut">${esc(l.motorista || "")}</td>
    <td class="mut">${esc(l.cupom || "")}</td>
    <td>${l.fatura ? `<a href="#/faturas" data-acao="fat-detalhe" data-id="${esc(l.fatura_id)}" style="font-weight:600;text-decoration:none">Fatura ${esc(l.fatura)}</a>` : `<span class="chip azul">A faturar</span>`}</td>
    <td>${l.tem_foto ? `<button class="btn ghost peq" data-acao="cupom" data-id="${esc(l.nota_id)}">Ver foto</button>` : `<span class="mut" style="font-size:13px">Sem foto</span>`}</td></tr>`).join("");
  const cart = r.linhas.map(l => `<div class="cart"><div class="l">${l.placa ? chip(l.placa, true) : "<span></span>"}<span class="val" style="font-size:20px">${brl(l.valor)}</span></div>
    <div style="font-weight:500">${l.litros != null ? num(l.litros, 1) + " L de " : ""}${esc(l.produto)}${l.preco != null ? " · " + brl(l.preco) + "/L" : ""}</div>
    <div class="mut" style="font-size:13px">${esc(horaBr(l.quando))}${l.motorista ? " · " + esc(l.motorista) : ""}${l.km ? " · km " + num(l.km) : ""} · cupom ${esc(l.cupom || "")}</div>
    <div class="l">${l.fatura ? `<span class="chip azul">Fatura ${esc(l.fatura)}</span>` : `<span class="chip azul">A faturar</span>`}
      ${l.tem_foto ? `<button class="btn ghost peq" data-acao="cupom" data-id="${esc(l.nota_id)}">Ver foto do cupom</button>` : `<span class="mut" style="font-size:13px">Sem foto</span>`}</div></div>`).join("");
  const rp = r.resumo_placas.map(x => `<tr><td>${chip(x.placa, true)}</td><td class="mut">${esc(x.veiculo)}</td><td class="d">${x.abastecimentos}</td><td class="d">${num(x.litros)} L</td>
    <td class="d val" style="font-size:15px">${brl(x.valor)}</td><td class="d mut">${x.km_rodados ? num(x.km_rodados) : "—"}</td><td class="d val" style="font-size:15px">${x.media_kml ? num(x.media_kml, 1) : "—"}</td></tr>`).join("");
  const opt = (arr, atual, rot0) => `<option value="">${rot0}</option>` + arr.map(v => `<option value="${esc(v)}"${String(atual || "") === String(v) ? " selected" : ""}>${esc(placaFmt(v))}</option>`).join("");
  raiz().innerHTML = moldura("Consumo", "Cada abastecimento da frota, com o cupom assinado e a fatura em que entrou.",
    `<button class="btn excel" data-acao="cons-xlsx">Exportar Excel</button><button class="btn pdf" data-acao="cons-pdf">Exportar PDF</button>`, `
    <section class="cartao pad" style="display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:14px;align-items:end">
      <label class="campo">De<input class="entrada" type="date" id="f-de" value="${esc(c.de)}"></label>
      <label class="campo">Até<input class="entrada" type="date" id="f-ate" value="${esc(c.ate)}"></label>
      <label class="campo">Placa<select class="entrada" id="f-placa">${opt(r.placas_disponiveis, c.placa, "Todas as placas")}</select></label>
      <label class="campo">Produto<select class="entrada" id="f-prod"><option value="">Todos</option><option value="diesel"${c.produto === "diesel" ? " selected" : ""}>Diesel</option><option value="gasolina"${c.produto === "gasolina" ? " selected" : ""}>Gasolina</option><option value="arla"${c.produto === "arla" ? " selected" : ""}>Arla</option></select></label>
      <label class="campo">Fatura<select class="entrada" id="f-fat"><option value="">Todas</option><option value="a_faturar"${c.fatura === "a_faturar" ? " selected" : ""}>A faturar</option>
        ${r.faturas_disponiveis.map(n => `<option value="${esc(n)}"${String(c.fatura) === String(n) ? " selected" : ""}>Fatura ${esc(n)}</option>`).join("")}</select></label>
      <button class="btn" data-acao="cons-aplicar">Aplicar</button>
    </section>
    <section style="display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:14px">
      <div class="cartao" style="padding:14px 18px"><div class="mut" style="font:600 11.5px var(--corpo);letter-spacing:.09em">ABASTECIMENTOS</div><div class="val" style="font-size:28px">${num(t.abastecimentos)}</div></div>
      <div class="cartao" style="padding:14px 18px"><div class="mut" style="font:600 11.5px var(--corpo);letter-spacing:.09em">LITROS</div><div class="val" style="font-size:28px">${num(t.litros)} L</div></div>
      <div class="cartao" style="padding:14px 18px"><div class="mut" style="font:600 11.5px var(--corpo);letter-spacing:.09em">VALOR</div><div class="val" style="font-size:28px">${brl(t.valor)}</div></div>
      <div class="cartao" style="padding:14px 18px"><div class="mut" style="font:600 11.5px var(--corpo);letter-spacing:.09em">PREÇO MÉDIO</div><div class="val" style="font-size:28px">${t.preco_medio ? brl(t.preco_medio) + " / L" : "—"}</div></div>
    </section>
    <section class="cartao" style="padding:18px 0 0">
      <div style="padding:0 20px 14px;display:flex;flex-wrap:wrap;gap:12px;align-items:center;justify-content:space-between">
        <div class="seg"><button data-acao="cons-visao" data-v="abast" class="${c.visao === "abast" ? "on" : ""}">Cada abastecimento</button><button data-acao="cons-visao" data-v="placas" class="${c.visao === "placas" ? "on" : ""}">Resumo por placa</button></div>
        <div class="mut">${esc(dataBr(r.de))} a ${esc(dataBr(r.ate))} · ${r.total_linhas} linha${r.total_linhas === 1 ? "" : "s"}</div>
      </div>
      ${c.visao === "abast" ? (r.linhas.length ? `<div class="rolar so-pc"><table class="t" style="min-width:1060px"><thead><tr><th>DATA E HORA</th><th>PLACA</th><th>PRODUTO</th><th class="d">LITROS</th><th class="d">R$/L</th><th class="d">VALOR</th><th class="d">KM</th><th>MOTORISTA</th><th>CUPOM</th><th>FATURA</th><th>CUPOM ASSINADO</th></tr></thead><tbody>${lin}</tbody></table></div>
          <div class="so-cel" style="padding:0 16px 12px;display:flex;flex-direction:column;gap:10px">${cart}</div>
          <div style="padding:14px 20px;display:flex;flex-wrap:wrap;gap:10px;align-items:center;justify-content:space-between" class="mut">
            <span>Página ${r.pagina} de ${r.paginas}</span>
            <span style="display:inline-flex;gap:6px"><button class="btn ghost peq" data-acao="cons-pag" data-v="${r.pagina - 1}" ${r.pagina <= 1 ? "disabled" : ""}>Anterior</button>
              <button class="btn ghost peq" data-acao="cons-pag" data-v="${r.pagina + 1}" ${r.pagina >= r.paginas ? "disabled" : ""}>Próxima</button></span></div>`
          : `<div class="vazio">Nenhum abastecimento neste filtro.</div>`)
        : (r.resumo_placas.length ? `<div class="rolar"><table class="t" style="min-width:760px"><thead><tr><th>PLACA</th><th>VEÍCULO</th><th class="d">ABASTEC.</th><th class="d">LITROS</th><th class="d">VALOR</th><th class="d">KM RODADOS</th><th class="d">MÉDIA KM/L</th></tr></thead><tbody>${rp}</tbody></table></div>
          <div class="mut" style="padding:14px 20px">A média km/L usa o odômetro informado em cada abastecimento (o primeiro abastecimento do período não entra na conta).</div>`
          : `<div class="vazio">Nenhuma placa com consumo neste filtro.</div>`)}
    </section>`);
}

async function abrirCupom(notaId) {
  janela(`<div class="esq" style="height:340px"></div>`, true);
  const r = await api("/empresas/api/cupom/" + encodeURIComponent(notaId) + "?" + eq());
  if (!r.ok) { fechaJanela(); aviso(r.erro || "Não consegui abrir o cupom."); return; }
  const itens = (r.itens || []).filter(i => i.litros != null);
  janela(`<div class="cab"><div><div class="mut" style="font:600 12px var(--corpo);letter-spacing:.1em">CUPOM ASSINADO</div><h2>Cupom ${esc(r.cupom)}</h2></div>
      <button class="btn ghost peq" data-acao="fecha">Fechar</button></div>
    <div style="display:flex;flex-wrap:wrap;gap:22px">
      <div class="foto" style="flex:1 1 320px;min-width:0">${r.foto_url ? `<img src="${esc(r.foto_url)}" alt="Foto do cupom assinado">` : `<span style="color:#D8E0E8;padding:20px;text-align:center">Este cupom não tem foto do comprovante assinado.</span>`}</div>
      <div style="flex:1 1 300px;min-width:0;display:flex;flex-direction:column;gap:14px">
        <dl class="dl">
          <dt>Data e hora</dt><dd>${esc(horaBr(r.quando))}</dd>
          <dt>Placa</dt><dd>${r.placa ? chip(r.placa, true) : "—"} ${esc(r.veiculo || "")}</dd>
          <dt>Motorista</dt><dd>${esc(r.motorista || "—")}</dd>
          ${itens.map(i => `<dt>Abastecimento</dt><dd>${esc(i.produto)}: ${num(i.litros, 1)} L a ${brl(i.preco)}</dd>`).join("")}
          <dt>Valor</dt><dd class="val">${brl(r.valor)}</dd>
          <dt>Odômetro</dt><dd>${r.km ? num(r.km) + " km" : "—"}</dd>
          <dt>Fatura</dt><dd>${r.fatura ? "Fatura " + esc(r.fatura) : `<span class="chip azul">A faturar</span>`}</dd>
        </dl>
        ${r.assinado_por ? `<div class="nota">Assinado por <b>${esc(r.assinado_por)}</b>${r.assinado_em ? " em " + esc(horaBr(r.assinado_em)) : ""}${r.codigo ? ". Código de autenticação <b>" + esc(r.codigo) + "</b>" : ""}.</div>` : ""}
        ${r.foto_url ? `<div class="acoes"><a class="btn" href="${esc(r.foto_url)}" target="_blank" rel="noopener">Abrir em tela cheia</a></div>` : ""}
      </div></div>`, true);
}

// ---------------------------------------------------------------- FATURAS
async function telaFaturas() {
  raiz().innerHTML = moldura("Faturas e boletos", "Pague, baixe a fatura em PDF ou em Excel e gere o boleto quando precisar.", "", esqueleto());
  const r = await api("/empresas/api/faturas?" + eq());
  if (S.rota !== "faturas") return;
  if (!r.ok) { raiz().innerHTML = moldura("Faturas e boletos", "", "", `<div class="aviso">${esc(r.erro || "Não consegui carregar as faturas.")}</div>`); return; }
  S.cache.faturas = r;
  const todas = r.abertas.concat(r.pagas);
  if (!S.faturas.sel || !todas.some(f => f.id === S.faturas.sel)) S.faturas.sel = (r.abertas[0] || r.pagas[0] || {}).id || null;
  desenhaFaturas();
  if (S.faturas.sel) carregaDetalhe(S.faturas.sel);
}
function sitFatura(f) {
  if (f.paga) return `<span class="chip verde">Paga em ${esc(dataBr(f.paga_em))}${f.forma ? " · " + esc(f.forma) : ""}</span>`;
  if (f.vencida) return `<span class="chip verm">Vencida há ${f.dias_atraso} dia${f.dias_atraso > 1 ? "s" : ""}</span>`;
  return `<span class="chip ouro">${f.dias_para_vencer === 0 ? "Vence hoje" : "Vence em " + f.dias_para_vencer + " dia" + (f.dias_para_vencer > 1 ? "s" : "")}</span>`;
}
function acoesFatura(f, compacto) {
  const bol = f.boleto.estado;
  const rot = bol === "disponivel" ? "Boleto" : (bol === "gerando" ? "Gerando…" : (bol === "vencido" ? "Gerar novo boleto" : "Gerar boleto"));
  return `<div style="display:flex;flex-wrap:wrap;gap:6px;align-items:center">
    ${!f.paga && f.pix_ligado ? `<button class="btn peq" data-acao="fat-pix" data-id="${esc(f.id)}">Pagar com Pix</button>` : ""}
    ${!f.paga ? `<button class="btn ghost peq" data-acao="fat-boleto" data-id="${esc(f.id)}">${esc(rot)}</button>` : ""}
    <button class="btn pdf peq" data-acao="fat-pdf" data-id="${esc(f.id)}">PDF</button>
    <button class="btn excel peq" data-acao="fat-xlsx" data-id="${esc(f.id)}" data-n="${esc(f.numero)}">Excel</button></div>`;
}
function desenhaFaturas() {
  const r = S.cache.faturas, aba = S.faturas.aba;
  const lista = aba === "abertas" ? r.abertas : (aba === "pagas" ? r.pagas : r.abertas.concat(r.pagas));
  const lin = lista.map(f => `<tr class="${f.id === S.faturas.sel ? "sel" : ""}">
      <td><button data-acao="fat-detalhe" data-id="${esc(f.id)}" style="border:0;background:transparent;padding:0;font:700 18px var(--titulo);color:var(--azul);text-decoration:underline;text-decoration-color:#B8C2CC">Fatura ${esc(f.numero)}</button></td>
      <td class="mut">${esc(dataBr(f.emissao))}</td><td class="mut">${esc(dataBr(f.vencimento))}</td>
      <td class="d"><span class="val">${brl(f.paga ? f.valor : f.valor_atualizado)}</span>${!f.paga && f.vencida && f.valor_atualizado !== f.valor ? `<div class="mut" style="font-size:12px">com multa e juros</div>` : ""}</td>
      <td>${sitFatura(f)}</td><td class="d mut">${f.qtd || ""}</td><td>${acoesFatura(f)}</td></tr>`).join("");
  const cart = lista.map(f => `<div class="cart"><div class="l"><div><div class="n val">Fatura ${esc(f.numero)}</div>
      <div class="mut" style="font-size:13px">Vence em ${esc(dataBr(f.vencimento))} · ${f.qtd || 0} abastecimentos</div></div>
      <div class="val" style="font-size:21px">${brl(f.paga ? f.valor : f.valor_atualizado)}</div></div>${sitFatura(f)}${acoesFatura(f)}
      <button class="btn ghost peq" style="align-self:flex-start" data-acao="fat-detalhe" data-id="${esc(f.id)}">Ver compras</button></div>`).join("");
  raiz().innerHTML = moldura("Faturas e boletos", "Pague, baixe a fatura em PDF ou em Excel e gere o boleto quando precisar.",
    `<button class="btn excel" data-acao="fats-xlsx">Lista em Excel</button><button class="btn pdf" data-acao="fats-pdf">Lista em PDF</button>`, `
    <section class="cartao" style="padding:18px 0 0">
      <div style="padding:0 20px 14px;display:flex;flex-wrap:wrap;gap:12px;align-items:center;justify-content:space-between">
        <div class="seg"><button data-acao="fats-aba" data-v="abertas" class="${aba === "abertas" ? "on" : ""}">Em aberto (${r.abertas.length})</button>
          <button data-acao="fats-aba" data-v="pagas" class="${aba === "pagas" ? "on" : ""}">Pagas (${r.pagas.length})</button>
          <button data-acao="fats-aba" data-v="todas" class="${aba === "todas" ? "on" : ""}">Todas (${r.abertas.length + r.pagas.length})</button></div>
        <div class="mut">Últimos 12 meses${r.taxas.multa_pct || r.taxas.juros_mes_pct ? ` · atraso: multa ${num(r.taxas.multa_pct, 1)}% e juros ${num(r.taxas.juros_mes_pct, 1)}% ao mês` : ""}</div>
      </div>
      ${lista.length ? `<div class="rolar so-pc"><table class="t" style="min-width:1000px"><thead><tr><th>FATURA</th><th>EMISSÃO</th><th>VENCIMENTO</th><th class="d">VALOR</th><th>SITUAÇÃO</th><th class="d">ABASTEC.</th><th>AÇÕES</th></tr></thead><tbody>${lin}</tbody></table></div>
        <div class="so-cel" style="padding:0 16px 14px;display:flex;flex-direction:column;gap:10px">${cart}</div>` : `<div class="vazio">Nenhuma fatura nesta lista.</div>`}
    </section>
    <section class="cartao pad" id="detalhe-fat"><div class="esq" style="height:240px"></div></section>`);
}
async function carregaDetalhe(id) {
  S.faturas.sel = id;
  const alvo = document.getElementById("detalhe-fat");
  if (!alvo) return;
  const r = await api("/empresas/api/faturas/" + encodeURIComponent(id) + "?" + eq());
  const a2 = document.getElementById("detalhe-fat");
  if (!a2 || S.faturas.sel !== id) return;
  if (!r.ok) { a2.innerHTML = `<div class="vazio">${esc(r.erro || "Não consegui abrir a fatura.")}</div>`; return; }
  const f = r.fatura;
  const bolMsg = { nao_gerado: "Esta fatura ainda não tem boleto. Gere agora: ele aparece aqui em segundos, sem precisar falar com o posto.",
                   vencido: "O boleto desta fatura venceu. Gere um novo boleto: ele vence hoje e já leva a multa e os juros do atraso. O boleto antigo é cancelado.",
                   disponivel: "Boleto disponível. Vence em " + dataBr(f.boleto.vencimento) + ".", gerando: "Gerando o boleto…", erro: "Não consegui gerar o boleto da última vez.", pago: "Boleto pago." }[f.boleto.estado] || "";
  a2.innerHTML = `<div style="display:flex;flex-wrap:wrap;gap:14px;align-items:flex-start;justify-content:space-between">
      <div><div class="mut" style="font:600 12px var(--corpo);letter-spacing:.1em">DETALHE DA FATURA</div>
        <h2 style="font-size:26px;margin-top:2px">Fatura ${esc(f.numero)} · ${brl(f.paga ? f.valor : f.valor_atualizado)}</h2>
        <div class="mut" style="margin-top:2px">Emitida em ${esc(dataBr(f.emissao))} · vencimento ${esc(dataBr(f.vencimento))} · ${r.totais.abastecimentos} abastecimentos</div></div>
      <div class="acoes"><button class="btn pdf" data-acao="fat-pdf" data-id="${esc(f.id)}">Baixar fatura em PDF</button>
        <button class="btn excel" data-acao="fat-xlsx" data-id="${esc(f.id)}" data-n="${esc(f.numero)}">Baixar compras em Excel</button></div></div>
    <div style="display:flex;flex-wrap:wrap;gap:24px;align-items:flex-start;margin-top:20px">
      <div style="flex:1 1 340px;min-width:0;display:flex;flex-direction:column;gap:14px">
        <div class="mut" style="font:600 12px var(--corpo);letter-spacing:.1em">PAGAMENTO</div>
        ${f.paga ? `<div class="nota" style="background:var(--verde-fundo);color:#0E5C35">Fatura paga em ${esc(dataBr(f.paga_em))}${f.forma ? " por " + esc(f.forma) : ""}.</div>` : `
          ${f.pix_ligado ? `<button class="btn bloco" data-acao="fat-pix" data-id="${esc(f.id)}">Pagar com Pix · ${brl(f.valor_atualizado)}</button>` : ""}
          <div class="nota" style="display:flex;flex-direction:column;gap:10px">
            <div style="display:flex;flex-wrap:wrap;gap:8px;justify-content:space-between;align-items:center"><b style="color:var(--txt)">Boleto</b>
              <span class="chip ${f.boleto.estado === "vencido" ? "verm" : (f.boleto.estado === "disponivel" ? "verde" : "ouro")}">${esc({ nao_gerado: "Ainda não gerado", vencido: "Vencido em " + dataBr(f.boleto.vencimento), disponivel: "Vence em " + dataBr(f.boleto.vencimento), gerando: "Gerando", erro: "Falhou" }[f.boleto.estado] || "")}</span></div>
            <div>${esc(bolMsg)}</div>
            <button class="btn ghost bloco" data-acao="fat-boleto" data-id="${esc(f.id)}">${f.boleto.estado === "disponivel" ? "Ver boleto" : (f.boleto.estado === "vencido" ? "Gerar novo boleto" : "Gerar boleto")}</button>
          </div>`}
        <div style="display:grid;grid-template-columns:1fr auto;gap:6px 16px">
          <div class="mut">Compras do período</div><div style="text-align:right">${brl(r.valor_bruto)}</div>
          <div class="mut">Acréscimo</div><div style="text-align:right">${brl(r.acrescimo)}</div>
          <div class="mut">Desconto</div><div style="text-align:right">${brl(r.desconto)}</div>
          ${!f.paga && f.vencida ? `<div class="mut">Multa por atraso</div><div style="text-align:right">${brl(f.multa)}</div><div class="mut">Juros (${f.dias_atraso} dias)</div><div style="text-align:right">${brl(f.juros)}</div>` : ""}
          <div style="font-weight:600;border-top:1px solid var(--borda);padding-top:8px">${!f.paga && f.vencida ? "Total hoje" : "Total da fatura"}</div>
          <div style="text-align:right;border-top:1px solid var(--borda);padding-top:8px" class="val">${brl(f.paga ? f.valor : f.valor_atualizado)}</div></div>
      </div>
      <div style="flex:2 1 460px;min-width:0">
        <div class="mut" style="font:600 12px var(--corpo);letter-spacing:.1em;margin-bottom:8px">COMPRAS POR PLACA</div>
        <div class="rolar"><table class="t" style="min-width:420px"><thead><tr><th>PLACA</th><th class="d">ABASTEC.</th><th class="d">LITROS</th><th class="d">VALOR</th></tr></thead><tbody>
          ${r.por_placa.map(p => `<tr><td>${chip(p.placa, true)}</td><td class="d">${p.abastecimentos}</td><td class="d">${num(p.litros)} L</td><td class="d val" style="font-size:16px">${brl(p.valor)}</td></tr>`).join("") || `<tr><td colspan="4" class="vazio">Sem detalhamento por placa.</td></tr>`}
        </tbody></table></div>
        <div style="margin-top:12px"><a href="#/consumo" data-acao="cons-da-fatura" data-n="${esc(f.numero)}" style="font-weight:600;text-decoration:none">Ver cada abastecimento desta fatura, com a foto do cupom</a></div>
      </div></div>`;
}

// ---------------------------------------------------------------- Pix da fatura
let _pixTimer = null, _pixId = null;
function pixParar() { if (_pixTimer) { clearInterval(_pixTimer); clearTimeout(_pixTimer); _pixTimer = null; } _pixId = null; }
async function fluxoPix(fid) {
  pixParar();
  janela(`<div class="cab"><h2>Pagar com Pix</h2><button class="btn ghost peq" data-acao="fecha">Fechar</button></div><div class="esq" style="height:260px"></div>`);
  const r = await api("/empresas/api/faturas/" + encodeURIComponent(fid) + "/pix", { empresa: emp() });
  if (!r.ok) { fechaJanela(); aviso(r.erro || "Não consegui gerar o Pix agora.", 4500); return; }
  _pixId = r.id;
  const id = r.id;
  let tent = 0;
  const passo = async () => {
    if (_pixId !== id) return;
    tent++;
    const s = await api("/empresas/api/pix/" + encodeURIComponent(id) + "?" + eq());
    if (_pixId !== id) return;
    if (s.status === "ativa") { mostraPix(s); return; }
    if (s.status === "paga") { pagoPix(s); return; }
    if (s.status === "erro" || s.status === "expirada") { fechaJanela(); aviso(s.erro || "Não consegui gerar o Pix agora.", 4500); return; }
    if (tent > 45) { fechaJanela(); aviso("O Pix está demorando para ser gerado. Tente de novo em instantes.", 4500); return; }
    _pixTimer = setTimeout(passo, 700);
  };
  _pixTimer = setTimeout(passo, 500);
}
async function mostraPix(s) {
  const id = s.id;
  janela(`<div class="cab"><div><div class="mut" style="font:600 12px var(--corpo);letter-spacing:.1em">PAGAR COM PIX</div><h2>${brl(s.valor)}</h2></div><button class="btn ghost peq" data-acao="fecha">Fechar</button></div>
    <div class="qr" id="pix-qr">…</div>
    <div class="nota" id="pix-estado" style="text-align:center">Abra o app do seu banco e leia o QR Code, ou copie o código. A baixa é automática.</div>
    <button class="btn bloco" data-acao="copiar" data-copiar="${esc(s.copia_cola || "")}" data-aviso="Código Pix copiado">Copiar código Pix</button>`);
  _pixId = id;
  fetch("/empresas/api/pix/" + encodeURIComponent(id) + "/qr.png?" + eq(), { headers: { Authorization: "Bearer " + S.token } })
    .then(r => r.ok ? r.blob() : null).then(b => { const q = document.getElementById("pix-qr"); if (b && q) q.innerHTML = `<img alt="QR Code Pix" src="${URL.createObjectURL(b)}">`; }).catch(() => {});
  _pixTimer = setInterval(async () => {
    if (_pixId !== id) return;
    const x = await api("/empresas/api/pix/" + encodeURIComponent(id) + "?" + eq());
    if (_pixId !== id) return;
    if (x.status === "paga") { pagoPix(x); }
    else if (x.status === "expirada" || x.status === "erro") { pixParar(); const e = document.getElementById("pix-estado"); if (e) e.textContent = x.erro || "Este Pix não está mais disponível."; }
  }, 1500);
}
function pagoPix(x) {
  pixParar();
  janela(`<div style="text-align:center;padding:10px 0 4px"><div style="font-size:42px;color:var(--verde)">✓</div>
    <h2 style="margin-top:6px">Pagamento recebido</h2><div class="mut" style="margin-top:6px">${brl(x.valor)} pagos. A baixa já foi feita.</div></div>
    <button class="btn bloco" data-acao="recarregar">Concluir</button>`);
}

// ---------------------------------------------------------------- Boleto da fatura
async function fluxoBoleto(fid) {
  janela(`<div class="cab"><h2>Boleto</h2><button class="btn ghost peq" data-acao="fecha">Fechar</button></div><div class="esq" style="height:200px"></div>`);
  const est = await api("/empresas/api/faturas/" + encodeURIComponent(fid) + "/boleto?" + eq());
  if (est.ok && est.estado === "disponivel") { mostraBoleto(fid, est.boleto, est.tem_pdf); return; }
  const r = await api("/empresas/api/faturas/" + encodeURIComponent(fid) + "/boleto", { empresa: emp() });
  if (!r.ok) { fechaJanela(); aviso(r.erro || "Não consegui gerar o boleto agora.", 4500); return; }
  janela(`<div class="cab"><h2>Gerando o boleto</h2><button class="btn ghost peq" data-acao="fecha">Fechar</button></div>
    <div class="nota">Registrando no banco. Fica pronto em instantes, vence hoje e já leva a multa e os juros do atraso.</div><div class="esq" style="height:120px"></div>`);
  let tent = 0;
  const passo = async () => {
    if (!document.getElementById("janela")) return;
    tent++;
    const e = await api("/empresas/api/faturas/" + encodeURIComponent(fid) + "/boleto?" + eq());
    if (e.ok && e.estado === "disponivel") { S.cache.faturas = null; mostraBoleto(fid, e.boleto, e.tem_pdf); return; }
    if (e.ok && e.estado === "erro") { fechaJanela(); aviso(e.erro || "Não consegui gerar o boleto agora.", 5000); return; }
    if (tent > 40) { fechaJanela(); aviso("O boleto está demorando. Veja de novo em instantes na lista de faturas.", 5000); return; }
    setTimeout(passo, 1500);
  };
  setTimeout(passo, 1200);
}
function mostraBoleto(fid, b, temPdf) {
  janela(`<div class="cab"><div><div class="mut" style="font:600 12px var(--corpo);letter-spacing:.1em">BOLETO</div><h2>${brl(b.valor)}</h2>
      <div style="margin-top:6px"><span class="chip verde">Vence em ${esc(dataBr(b.vencimento))}</span></div></div>
      <button class="btn ghost peq" data-acao="fecha">Fechar</button></div>
    <div><div class="mut" style="font:600 12px var(--corpo);letter-spacing:.1em;margin-bottom:6px">LINHA DIGITÁVEL</div>
      <div class="linha-dig">${esc(b.linha_digitavel || "Linha digitável ainda não disponível.")}</div></div>
    <button class="btn ghost bloco" data-acao="copiar" data-copiar="${esc(b.linha_digitavel || "")}" data-aviso="Linha digitável copiada">Copiar linha digitável</button>
    ${temPdf ? `<button class="btn bloco" data-acao="bol-pdf" data-id="${esc(fid)}">Baixar boleto em PDF</button>` : `<div class="mut" style="font-size:13.5px">O PDF do boleto fica pronto em instantes. Feche e abra de novo para baixar.</div>`}
    <div class="mut" style="font-size:13.5px;line-height:19px">Depois de pago, a compensação leva até 1 dia útil e a fatura sai da lista de abertas sozinha.</div>`);
}
async function abrirArquivo(fid, tipo) {
  for (let t = 0; t < 8; t++) {
    const r = await api("/empresas/api/faturas/" + encodeURIComponent(fid) + "/arquivo?" + eq() + "&tipo=" + tipo);
    if (r.ok && r.url) { window.open(r.url, "_blank", "noopener"); return; }
    if (r.ok && r.gerando) { if (t === 0) aviso("Gerando o PDF…", 3000); await new Promise(res => setTimeout(res, 3000)); continue; }
    aviso(r.erro || "Não consegui abrir o documento.", 4500);
    return;
  }
  aviso("O documento ainda está sendo gerado. Tente de novo em instantes.", 4500);
}

// ---------------------------------------------------------------- tratador único de cliques
const ACOES = {
  sair: () => sair(false),
  fecha: () => fechaJanela(),
  recarregar: () => { fechaJanela(); S.cache = {}; rotear(); },
  copiar: (el) => {
    const orig = el.getAttribute("data-rot") || el.textContent;
    el.setAttribute("data-rot", orig);
    copiarTexto(el.getAttribute("data-copiar") || "", el.getAttribute("data-aviso") || "Copiado", () => {
      el.textContent = "✓ Copiado"; el.classList.add("copiado");
      setTimeout(() => { el.textContent = orig; el.classList.remove("copiado"); }, 2200);
    });
  },
  "vis-met": (el) => { S.visMet = el.dataset.v; desenhaVisao(); },
  "vis-per": (el) => { S.visPer = el.dataset.v; desenhaVisao(); },
  "nova-placa": () => modalPlaca(null),
  "nova-placa-rota": (el, e) => { e.preventDefault(); location.hash = "#/placas"; setTimeout(() => modalPlaca(null), 300); },
  "placa-editar": (el) => modalPlaca(el.dataset.id),
  "placa-salvar": (el) => salvarPlaca(el.dataset.id || null, el),
  "placas-filtro": (el) => { S.placas.filtro = el.dataset.v; desenhaPlacas(); },
  "placa-chave": async (el) => {
    el.disabled = true;
    const quer = el.dataset.v === "1";
    const r = await api("/empresas/api/placas/" + encodeURIComponent(el.dataset.id) + "/ativo", { empresa: emp(), ativo: quer });
    if (!r.ok) { aviso(r.erro || "Não consegui alterar a placa.", 4500); el.disabled = false; return; }
    aviso(quer ? "Placa ativada." : "Placa desativada: o caixa deixa de aceitá-la a prazo.");
    S.cache.visao = null;
    if (S.rota === "placas") telaPlacas(); else telaVisao();
  },
  "importar-placas": () => modalImportar(),
  "imp-conferir": () => conferirImport(false),
  "imp-gravar": () => conferirImport(true),
  "cons-aplicar": () => {
    const c = S.consumo;
    c.de = document.getElementById("f-de").value || iniMesIso(); c.ate = document.getElementById("f-ate").value || hojeIso();
    c.placa = document.getElementById("f-placa").value; c.produto = document.getElementById("f-prod").value; c.fatura = document.getElementById("f-fat").value;
    c.pagina = 1; telaConsumo();
  },
  "cons-visao": (el) => { S.consumo.visao = el.dataset.v; desenhaConsumo(); },
  "cons-pag": (el) => { S.consumo.pagina = Number(el.dataset.v) || 1; telaConsumo(); },
  "cons-xlsx": () => baixar("/empresas/api/consumo/export?" + filtroConsumoQ({ formato: "xlsx" }), "consumo.xlsx"),
  "cons-pdf": () => baixar("/empresas/api/consumo/export?" + filtroConsumoQ({ formato: "pdf" }), "consumo.pdf"),
  "cons-da-fatura": (el) => { S.consumo.fatura = el.dataset.n; S.consumo.pagina = 1; },
  cupom: (el) => abrirCupom(el.dataset.id),
  "fats-aba": (el) => { S.faturas.aba = el.dataset.v; desenhaFaturas(); if (S.faturas.sel) carregaDetalhe(S.faturas.sel); },
  "fat-detalhe": (el, e) => {
    S.faturas.sel = el.dataset.id;
    if (S.rota !== "faturas") { S.cache.faturas = null; return; }                    // o link troca para #/faturas e a tela carrega o detalhe
    e.preventDefault();
    desenhaFaturas(); carregaDetalhe(el.dataset.id);
    const d = document.getElementById("detalhe-fat"); if (d) d.scrollIntoView({ behavior: "smooth", block: "start" });
  },
  "fat-pix": (el) => fluxoPix(el.dataset.id),
  "fat-boleto": (el) => fluxoBoleto(el.dataset.id),
  "fat-pdf": (el) => abrirArquivo(el.dataset.id, "fatura"),
  "bol-pdf": (el) => abrirArquivo(el.dataset.id, "boleto"),
  "fat-xlsx": (el) => baixar("/empresas/api/faturas/" + encodeURIComponent(el.dataset.id) + "/export?" + eq(), "fatura-" + (el.dataset.n || "") + "-compras.xlsx"),
  "fats-xlsx": () => baixar("/empresas/api/faturas/export?" + eq() + "&formato=xlsx&aba=" + S.faturas.aba, "faturas.xlsx"),
  "fats-pdf": () => baixar("/empresas/api/faturas/export?" + eq() + "&formato=pdf&aba=" + S.faturas.aba, "faturas.pdf"),
};
document.addEventListener("click", e => {
  const el = e.target.closest("[data-acao]");
  if (!el) return;
  const f = ACOES[el.dataset.acao];
  if (f) { if (el.tagName !== "A") e.preventDefault(); f(el, e); }
});
document.addEventListener("change", e => {
  if (e.target && e.target.id === "sel-emp") {
    S.empresa = S.eu.empresas.find(x => x.id === e.target.value) || S.empresa;
    LS.set("psn_emp_empresa", S.empresa.id);
    S.cache = {};
    rotear();
  }
});
document.addEventListener("keydown", e => { if (e.key === "Escape") fechaJanela(); });

iniciar();
