// ============================================================
// App Postos SN — ACESSO DA EQUIPE (etapa de LAYOUT, 10/10/2026)
// ------------------------------------------------------------
// O dono e os gerentes entram com o mesmo e-mail e senha do sistema (retaguarda).
// Decidido pelo Ronan:
//  * 05/10: a primeira versão da equipe são as APROVAÇÕES do caixa (desconto, crédito
//    a prazo, aferição);
//  * 10/10: aprova com UM toque; o pedido VENCE em 1 minuto sem resposta; o gerente vê
//    o mesmo que o dono, mas o dono liga e desliga, por perfil, o que ele pode APROVAR
//    e o que ele pode VER (tela Permissões do app).
// As rotas /app/equipe/api/* AINDA NÃO EXISTEM no servidor: por enquanto as telas só
// abrem na bancada (_ferramentas/app_bancada), com dados de exemplo. Quando existirem,
// é o SERVIDOR que deixa de mandar o que o perfil não pode ver — esconder aqui é só
// acabamento.
// Tudo que vem do servidor passa por esc() antes de virar HTML.
// ============================================================

const EQ_ROTAS = ["eq-painel", "eq-aprovacoes", "eq-pedido", "eq-postos", "eq-posto", "eq-conta", "eq-permissoes"];
EQ_ROTAS.forEach(r => PUBLICAS.push(r));     // não usam a conta de cliente: cada tela confere o login da equipe
const EQ = { eu: null };
const EQ_TIPOS = {
  desconto: { nome: "Desconto no caixa", ico: "desconto", pode: "aprovar_desconto" },
  prazo: { nome: "Crédito a prazo", ico: "prazo", pode: "aprovar_prazo" },
  afericao: { nome: "Aferição", ico: "bomba", pode: "aprovar_afericao" },
};
// o que o dono liga e desliga por perfil (a mesma lista vai para o retaguarda › Perfis)
const EQ_PERMISSOES = [
  ["Pode aprovar", [["aprovar_desconto", "Desconto no caixa"], ["aprovar_prazo", "Venda a prazo acima do limite"], ["aprovar_afericao", "Aferição"]]],
  ["Pode ver", [["ver_vendas", "Vendas do dia em reais"], ["ver_litros", "Litros e número de cupons"], ["ver_tanques", "Nível dos tanques"],
    ["ver_recebido", "Recebido por forma de pagamento"], ["ver_precos", "Preço na bomba"], ["ver_alertas", "Alertas de tanque baixo, sonda parada e posto sem resposta"]]],
];

function tokEq() { return LS.get("psn_equipe") || ""; }

async function apiEq(caminho, corpo) {
  let r;
  try {
    r = await fetch(API + caminho, {
      method: corpo ? "POST" : "GET",
      headers: { "Content-Type": "application/json", Authorization: "Bearer " + tokEq() },
      body: corpo ? JSON.stringify(corpo) : undefined,
    });
  } catch (e) {
    return { http: 0, erro: "Sem conexão com a internet. Tente de novo." };
  }
  let j;
  try { j = await r.json(); } catch (e) { j = { erro: "Resposta inválida do servidor (" + r.status + ")." }; }
  if (Array.isArray(j)) { j.http = r.status; return j; }
  if (r.status === 401 && tokEq()) eqSair();
  return Object.assign({ http: r.status }, j);
}

function eqSair() {
  LS.del("psn_equipe");
  EQ.eu = null;
  if (location.hash.indexOf("#/equipe") !== 0) ir("#/equipe");
}

async function eqEu() {
  if (!EQUIPE_NO_AR || !tokEq()) { ir("#/equipe"); return null; }
  if (EQ.eu) return EQ.eu;
  const r = await apiEq("/app/equipe/api/eu");
  if (!r.ok) { eqSair(); return null; }
  EQ.eu = r;
  eqBarraPermissoes();
  return r;
}
// sem a lista de permissões (o dono) = pode tudo
function eqPode(k) { const p = EQ.eu && EQ.eu.pode; return !p || p[k] !== false; }
function eqAprova() { return Object.keys(EQ_TIPOS).some(t => eqPode(EQ_TIPOS[t].pode)); }
function eqVePostos() { return eqPode("ver_vendas") || eqPode("ver_litros") || eqPode("ver_tanques"); }

function barraEquipe(nome) {
  const b = document.getElementById("barra-eq");
  if (!b) return;
  const mostra = EQ_ROTAS.includes(nome) && !!tokEq();
  b.style.display = mostra ? "" : "none";
  if (mostra) document.querySelector(".app").classList.remove("sem-barra");
  const ativo = { "eq-pedido": "eq-aprovacoes", "eq-posto": "eq-postos", "eq-permissoes": "eq-conta" }[nome] || nome;
  b.querySelectorAll("[data-aba]").forEach(x => x.classList.toggle("on", x.dataset.aba === ativo));
}
function eqBarraPermissoes() {       // quem não aprova nada não tem a aba Aprovações; quem não vê nada dos postos não tem a aba Postos
  const b = document.getElementById("barra-eq");
  if (!b) return;
  const some = { "eq-aprovacoes": !eqAprova(), "eq-postos": !eqVePostos() };
  b.querySelectorAll("[data-aba]").forEach(x => { if (x.dataset.aba in some) x.style.display = some[x.dataset.aba] ? "none" : ""; });
}
function eqContador(n) {
  const c = document.getElementById("eq-pend-n");
  if (c) c.textContent = n > 0 ? (n > 99 ? "99+" : String(n)) : "";
}

function haQuanto(iso) {
  const min = Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 60000));
  if (min < 1) return "agora";
  if (min < 60) return "há " + min + " min";
  const h = Math.floor(min / 60);
  return h < 24 ? "há " + h + " h" : dataBr(iso);
}
function saudacao() { const h = new Date().getHours(); return h < 12 ? "Bom dia," : h < 18 ? "Boa tarde," : "Boa noite,"; }

// ---------------------------------------------------------------- relógio dos pedidos (vencem em 1 minuto)
// Atualiza todo [data-vence] da tela a cada segundo e para sozinho quando a tela muda.
let _eqRelogio = null;
function eqRelogio(aoVencer) {
  if (_eqRelogio) clearInterval(_eqRelogio);
  let avisou = false;
  const tic = () => {
    const els = document.querySelectorAll("[data-vence]");
    if (!els.length) { clearInterval(_eqRelogio); _eqRelogio = null; return; }
    let algumVivo = false;
    els.forEach(e => {
      const s = Math.ceil((new Date(e.dataset.vence).getTime() - Date.now()) / 1000);
      if (s > 0) algumVivo = true;
      if (e.dataset.barra) { e.style.width = Math.max(0, Math.min(100, s / (+e.dataset.barra) * 100)) + "%"; e.classList.toggle("fim", s <= 15); return; }
      e.textContent = s > 0 ? "vence em " + Math.floor(s / 60) + ":" + String(s % 60).padStart(2, "0") : "venceu";
      const linha = e.closest(".linha");
      if (linha) linha.classList.toggle("vencido", s <= 0);
    });
    if (!algumVivo && !avisou) { avisou = true; if (aoVencer) aoVencer(); }
  };
  tic();
  _eqRelogio = setInterval(tic, 1000);
}

// ================================================================ ENTRAR (equipe)
function telaEquipe() {
  if (!EQUIPE_NO_AR) {                          // rotas do servidor ainda não publicadas: nada de formulário que não funciona
    el().innerHTML = `<div class="entrar tela">
      <img class="marca" src="${LOGO_AZUL}" alt="Rede SN" style="width:110px">
      <h1 style="text-align:center;font-size:1.3rem;margin:8px 0 6px">Acesso da equipe</h1>
      <div class="card" style="padding:18px;color:var(--mut);font-size:.92rem;text-align:center">Em breve.</div>
      <button class="btn sec" onclick="ir('#/entrar')">Voltar</button>
    </div>`;
    return;
  }
  if (tokEq()) { ir("#/eq-painel"); return; }
  el().innerHTML = `<div class="entrar tela">
    <img class="marca" src="${LOGO_AZUL}" alt="Rede SN" style="width:110px">
    <h1 style="text-align:center;font-size:1.3rem;margin:8px 0 4px">Acesso da equipe</h1>
    <div style="text-align:center;color:var(--mut);font-size:.9rem;margin-bottom:8px">Entre com o mesmo e-mail e senha do sistema.</div>
    <div class="campo"><label for="g-email">E-mail</label><input id="g-email" type="email" inputmode="email" autocomplete="username" placeholder="nome@exemplo.com"></div>
    <div class="campo"><label for="g-senha">Senha</label><input id="g-senha" type="password" autocomplete="current-password"></div>
    <button class="btn" id="g-btn">Entrar</button>
    <div class="msg" id="g-msg"></div>
    <div class="equipe-link">É cliente? <a class="link" href="#/entrar">Entrar com CPF</a></div>
  </div>`;
  const entrar = async () => {
    const m = document.getElementById("g-msg");
    m.className = "msg"; m.textContent = "Entrando…";
    const r = await apiEq("/app/equipe/api/entrar", { email: document.getElementById("g-email").value.trim(), senha: document.getElementById("g-senha").value });
    if (r.token) { LS.set("psn_equipe", r.token); EQ.eu = null; ir("#/eq-painel"); return; }
    m.className = "msg erro"; m.textContent = r.erro || "Não consegui entrar.";
  };
  document.getElementById("g-btn").onclick = entrar;
  document.getElementById("g-senha").addEventListener("keydown", e => { if (e.key === "Enter") entrar(); });
}

// ================================================================ PAINEL
function eqTanques(tanques) {
  const ts = Array.isArray(tanques) ? tanques : [];
  if (!ts.length || !eqPode("ver_tanques")) return "";
  return `<div class="tanques">${ts.map(t => {
    const pct = Math.max(0, Math.min(100, Math.round(t.pct || 0)));
    const curto = String(t.produto || "").replace(/^diesel\s+/i, "");     // no cartão cabe "S500 55%", não "Diesel S500 55%"
    return `<div class="tq ${t.baixo ? "baixo" : ""}"><div class="nivel"><i style="width:${pct}%"></i></div><div class="rot">${esc(curto)} ${pct}%</div></div>`;
  }).join("")}</div>`;
}
function eqNumeros(p) {
  const k = [];
  if (eqPode("ver_vendas")) k.push(["Vendas hoje", brl(p.vendas)]);
  if (eqPode("ver_litros")) k.push(["Litros", num(Math.round(p.litros || 0)) + " L"], ["Cupons", num(p.cupons)]);
  if (!k.length) return "";
  return `<div class="kpis" style="grid-template-columns:${k.length === 3 ? "1.3fr 1fr 1fr" : "repeat(" + k.length + ",1fr)"}">${k.map(x =>
    `<div class="kpi"><div class="rot">${x[0]}</div><div class="val">${x[1]}</div></div>`).join("")}</div>`;
}
function eqSituacao(p) {
  if (p.online === false) return ["fora", p.sem_resposta || "sem resposta"];
  if (p.alerta) return ["aviso", p.alerta];
  return ["", p.turno ? "turno de " + primeiroNome(p.turno) : "sem turno aberto"];
}
function eqCartaoPosto(p) {
  const st = eqSituacao(p);
  return `<button class="card posto-card" onclick="ir('#/eq-posto/${esc(p.id)}')">
    <div class="cab"><i class="bola ${st[0]}"></i><b>${esc(p.nome)}</b><span>${esc(st[1])}</span></div>
    ${eqNumeros(p)}${eqTanques(p.tanques)}</button>`;
}
function eqTotalRede(postos) {
  const v = postos.reduce((s, p) => s + Number(p.vendas || 0), 0), l = postos.reduce((s, p) => s + Number(p.litros || 0), 0);
  const partes = [];
  if (eqPode("ver_vendas")) partes.push(`<b style="color:var(--txt)">${brl(v)}</b>`);
  if (eqPode("ver_litros")) partes.push(`${num(Math.round(l))} L`);
  const quem = EQ.eu && EQ.eu.master ? "Rede" : postos.length > 1 ? "Seus postos" : "Posto";
  return partes.length ? `<div class="vazio" style="padding:0 2px 10px">${quem} até agora: ${partes.join(" · ")}</div>` : "";
}

async function telaEqPainel() {
  const eu = await eqEu();
  if (!eu) return;
  const r = await apiEq("/app/equipe/api/painel");
  const postos = Array.isArray(r.postos) ? r.postos : [], alertas = eqPode("ver_alertas") && Array.isArray(r.alertas) ? r.alertas : [];
  const pend = Number(r.pendentes || 0);
  eqContador(pend);
  el().innerHTML = `<div class="tela">
    <div class="topo-azul"><div class="topo-linha">
      <div class="topo-logo"><img src="${LOGO_AZUL}" alt="Rede SN"></div>
      <div><div class="topo-ola">${saudacao()}</div><div class="topo-nome">${esc(primeiroNome(eu.nome))}</div></div>
      <span class="papel">${esc(eu.papel_nome || "Equipe")}</span>
    </div></div>
    ${eqAprova() ? `<button class="pend ${pend ? "" : "zero"}" onclick="ir('#/eq-aprovacoes')">
      <div class="n">${num(pend)}</div>
      <div class="meio"><div class="t">${pend === 1 ? "pedido esperando você" : pend ? "pedidos esperando você" : "Nenhum pedido esperando"}</div>
        <div class="s">${esc(pend ? (r.pendentes_resumo || "Toque para ver") : "O caixa chama aqui quando precisar de você.")}</div></div>
      <span class="seta">${ICO.seta(20)}</span></button>` : '<div style="height:14px"></div>'}
    ${alertas.length ? `<div class="secao"><div class="secao-tit"><h2>Alertas</h2></div><div class="card lista">${alertas.map(a => `
      <button class="linha" onclick="${a.posto_id ? `ir('#/eq-posto/${esc(a.posto_id)}')` : ""}"><span class="ic" style="background:var(--ouro-fundo);color:#8A5A00">${ICO.alerta(20)}</span>
        <div class="meio"><div class="t">${esc(a.titulo)}</div><div class="s">${esc(a.texto || "")}</div></div></button>`).join("")}</div></div>` : ""}
    ${eqVePostos() ? `<div class="secao"><div class="secao-tit"><h2>Hoje nos postos</h2><a href="#/eq-postos">Ver todos</a></div>
      ${postos.length ? eqTotalRede(postos) + postos.map(eqCartaoPosto).join("") : '<div class="card vazio" style="padding:16px">Nenhum posto liberado para o seu perfil.</div>'}</div>` : ""}
  </div>`;
}

// ================================================================ APROVAÇÕES
async function telaEqAprovacoes(aba) {
  const eu = await eqEu();
  if (!eu) return;
  const resolvidas = aba === "resolvidas";
  const r = await apiEq("/app/equipe/api/aprovacoes?situacao=" + (resolvidas ? "resolvidas" : "pendentes"));
  const lst = Array.isArray(r) ? r : [];
  if (!resolvidas) eqContador(lst.length);
  const ST = { aprovado: ["Aprovado", "verde"], recusado: ["Recusado", "cinza"], expirado: ["Venceu", "ouro"] };
  const linha = p => {
    const t = EQ_TIPOS[p.tipo] || { nome: p.tipo, ico: "aprovar" }, st = ST[p.situacao];
    const quando = !st && p.vence_em ? `<b data-vence="${esc(p.vence_em)}" style="color:#8A5A00"></b>` : esc(haQuanto(p.quando));
    return `<button class="linha" onclick="ir('#/eq-pedido/${esc(p.id)}')"><span class="ic">${ICO[t.ico](20)}</span>
      <div class="meio"><div class="t">${esc(p.titulo || t.nome)}</div><div class="s">${esc(p.posto)} · ${esc(primeiroNome(p.pediu))} · ${quando}</div></div>
      ${st ? `<span class="tag ${st[1]}">${st[0]}</span>` : `<span class="seta">${ICO.seta(18)}</span>`}</button>`;
  };
  el().innerHTML = `<div class="tela">${topoSimples("Aprovações", false)}
    <div class="secao" style="padding-top:2px">
      <div class="seg"><button class="${resolvidas ? "" : "on"}" onclick="ir('#/eq-aprovacoes')">Pendentes</button><button class="${resolvidas ? "on" : ""}" onclick="ir('#/eq-aprovacoes/resolvidas')">Resolvidas</button></div>
    </div>
    <div class="secao">${lst.length ? `<div class="card lista">${lst.map(linha).join("")}</div>`
      : `<div class="card vazio" style="padding:16px">${resolvidas ? "Nenhum pedido resolvido ainda." : "Nenhum pedido esperando. Quando o caixa precisar de você, o celular avisa."}</div>`}
      ${resolvidas ? "" : '<div class="vazio" style="text-align:center">O caixa espera a resposta por 1 minuto. Depois disso o pedido vence e ele precisa pedir de novo.</div>'}</div>
  </div>`;
  if (!resolvidas && lst.length) eqRelogio();
}

async function telaEqPedido(id) {
  const eu = await eqEu();
  if (!eu) return;
  const p = await apiEq("/app/equipe/api/aprovacoes/" + encodeURIComponent(id));
  if (!p.ok) {
    el().innerHTML = `<div class="tela">${topoSimples("Pedido", true)}<div class="secao"><div class="card vazio" style="padding:16px">${esc(p.erro || "Pedido não encontrado.")}</div></div></div>`;
    return;
  }
  const t = EQ_TIPOS[p.tipo] || { nome: p.tipo, ico: "aprovar" };
  const pendente = p.situacao === "pendente";
  const SELO = { aprovado: ["Aprovado", "verde"], recusado: ["Recusado", "cinza"], expirado: ["Venceu sem resposta", "cinza"] }[p.situacao] || [p.situacao, "cinza"];
  const total = Math.max(1, Math.round((new Date(p.vence_em).getTime() - new Date(p.quando).getTime()) / 1000) || 60);
  el().innerHTML = `<div class="tela">${topoSimples(t.nome, true)}
    <div class="secao" style="padding-top:2px"><div class="card resumo">
      ${pendente ? `<span class="tag ouro" id="pd-selo">Esperando você · <span data-vence="${esc(p.vence_em)}"></span></span>
        <div class="tempo"><i data-vence="${esc(p.vence_em)}" data-barra="${total}"></i></div>` : `<span class="tag ${SELO[1]}">${esc(SELO[0])}</span>`}
      <div class="grande">${esc(p.destaque || "")}</div>
      <div style="color:var(--mut)">${esc(p.resumo || "")}</div>
    </div></div>
    <div class="secao"><div class="card">${(p.linhas || []).map(l => `<div class="par"><span>${esc(l[0])}</span><b class="${l[2] === "alerta" ? "alerta" : ""}">${esc(l[1])}</b></div>`).join("")}</div></div>
    ${p.motivo ? `<div class="secao"><div class="secao-tit"><h2>O que o caixa escreveu</h2></div><div class="card" style="padding:14px 16px">${esc(p.motivo)}</div></div>` : ""}
    ${pendente ? `<div class="acoes"><button class="btn recusa" id="pd-nao">Recusar</button><button class="btn verde" id="pd-sim">Aprovar</button></div><div class="msg" id="pd-msg"></div>`
      : `<div class="vazio" style="text-align:center">${esc(p.resolvido || "")}</div>`}
  </div>`;
  if (!pendente) return;
  const decidir = async (aprovar, motivo) => {
    const r = await apiEq("/app/equipe/api/aprovacoes/" + encodeURIComponent(id) + "/decidir", { aprovar, motivo: motivo || "" });
    fecharFolha();
    if (!r.ok) { const m = document.getElementById("pd-msg"); if (m) { m.className = "msg erro"; m.textContent = r.erro || "Não consegui registrar. Tente de novo."; } return; }
    toast(aprovar ? "Aprovado. O caixa já foi avisado." : "Recusado. O caixa já foi avisado.");
    ir("#/eq-aprovacoes");
  };
  document.getElementById("pd-sim").onclick = () => decidir(true);          // um toque só (decisão do Ronan, 10/10)
  document.getElementById("pd-nao").onclick = () => {
    folha(`<h2 style="font-size:1.15rem">Recusar o pedido?</h2>
      <div style="color:var(--mut);margin-top:6px">O caixa recebe a recusa na hora. Se quiser, diga o motivo.</div>
      <div class="campo"><label for="pd-motivo">Motivo (opcional)</label><input id="pd-motivo" maxlength="120" placeholder="Ex.: cliente com fatura vencida"></div>
      <button class="btn verm" id="pd-rec">Recusar</button><button class="btn sec" onclick="fecharFolha()">Voltar</button>`);
    document.getElementById("pd-rec").onclick = () => decidir(false, document.getElementById("pd-motivo").value.trim());
  };
  eqRelogio(() => {                                                          // passou 1 minuto: não dá mais para responder
    fecharFolha();
    const selo = document.getElementById("pd-selo"), m = document.getElementById("pd-msg");
    if (selo) { selo.className = "tag cinza"; selo.textContent = "Venceu sem resposta"; }
    const acoes = document.querySelector(".acoes");
    if (acoes) acoes.style.display = "none";
    if (m) { m.className = "msg"; m.textContent = "Passou 1 minuto sem resposta. O caixa precisa pedir de novo."; }
  });
}

// ================================================================ POSTOS
async function telaEqPostos() {
  const eu = await eqEu();
  if (!eu) return;
  const r = await apiEq("/app/equipe/api/painel");
  const postos = Array.isArray(r.postos) ? r.postos : [];
  el().innerHTML = `<div class="tela">${topoSimples("Postos", false)}
    <div class="secao" style="padding-top:2px">${postos.length ? eqTotalRede(postos) + postos.map(eqCartaoPosto).join("")
      : '<div class="card vazio" style="padding:16px">Nenhum posto liberado para o seu perfil.</div>'}</div></div>`;
}

async function telaEqPosto(id) {
  const eu = await eqEu();
  if (!eu) return;
  const p = await apiEq("/app/equipe/api/posto/" + encodeURIComponent(id));
  if (!p.ok) { ir("#/eq-postos"); return; }
  const st = eqSituacao(p);
  const lista = (k, pode) => eqPode(pode) && Array.isArray(p[k]) ? p[k] : [];
  const formas = lista("formas", "ver_recebido"), tanques = lista("tanques", "ver_tanques"), precos = lista("precos", "ver_precos");
  el().innerHTML = `<div class="tela">${topoSimples(p.nome, true)}
    <div class="secao" style="padding-top:2px"><div class="card posto-card" style="margin:0">
      <div class="cab"><i class="bola ${st[0]}"></i><b>${esc(p.turno ? "Turno de " + primeiroNome(p.turno) : "Sem turno aberto")}</b><span>${esc(p.turno_desde ? "desde " + horaBr(p.turno_desde) : st[1])}</span></div>
      ${eqNumeros(p)}</div></div>
    ${tanques.length ? `<div class="secao"><div class="secao-tit"><h2>Tanques</h2><span style="color:var(--mut);font-size:.8rem">${esc(p.medido_em ? "medido às " + horaBr(p.medido_em) : "")}</span></div>
      <div class="card">${tanques.map(t => {
        const pct = Math.max(0, Math.min(100, Math.round(t.pct || 0)));
        return `<div class="tanque-linha ${t.baixo ? "baixo" : ""}"><div class="cab"><span>${esc(t.produto)}</span><span class="dir">${num(Math.round(t.litros || 0))} L</span></div>
          <div class="nivel"><i style="width:${pct}%"></i></div><div class="rot">${pct}% de ${num(t.capacidade)} L${t.baixo ? " · nível baixo" : ""}</div></div>`;
      }).join("")}</div></div>` : ""}
    ${formas.length ? `<div class="secao"><div class="secao-tit"><h2>Recebido hoje</h2></div><div class="card">${formas.map(f => `<div class="par"><span>${esc(f[0])}</span><b>${brl(f[1])}</b></div>`).join("")}</div></div>` : ""}
    ${precos.length ? `<div class="secao"><div class="secao-tit"><h2>Preço na bomba</h2></div><div class="card">${precos.map(f => `<div class="par"><span>${esc(f[0])}</span><b>${brl(f[1])}/L</b></div>`).join("")}</div></div>` : ""}
  </div>`;
}

// ================================================================ CONTA (equipe)
async function telaEqConta() {
  const eu = await eqEu();
  if (!eu) return;
  const av = eu.avisos || {};
  const curtos = { desconto: "desconto", prazo: "a prazo", afericao: "aferição" };
  const tipos = Object.keys(EQ_TIPOS).filter(t => eqPode(EQ_TIPOS[t].pode)).map(t => curtos[t]);
  const lista = tipos.length > 1 ? tipos.slice(0, -1).join(", ") + " e " + tipos[tipos.length - 1] : (tipos[0] || "");
  const meusTipos = lista.charAt(0).toUpperCase() + lista.slice(1);       // "Desconto, a prazo e aferição" — só o que o perfil aprova
  const chave = (id, txt, sub, ligado) => `<button class="linha" onclick="eqAviso('${id}')"><div class="meio"><div class="t">${txt}</div><div class="s">${sub}</div></div><span class="chave ${ligado ? "on" : ""}" id="eq-av-${id}"></span></button>`;
  const ir_ = (ic, txt, acao) => `<button class="linha" onclick="${acao}"><span class="ic">${ICO[ic](20)}</span><div class="meio"><div class="t">${txt}</div></div><span class="seta">${ICO.seta(18)}</span></button>`;
  el().innerHTML = `<div class="tela">
    <div class="perfil-topo"><div class="avatar">${esc(iniciais(eu.nome))}</div>
      <div><div style="font-size:1.35rem;font-weight:800;letter-spacing:-.01em">${esc(primeiroNome(eu.nome))}</div>
        <div style="color:var(--mut);font-size:.84rem">${esc(eu.papel_nome || "Equipe")} · ${esc(eu.email || "")}</div></div></div>
    ${eu.master ? `<div class="grupo-tit">Administração</div>
      <div class="secao" style="padding-top:0"><div class="card lista">${ir_("escudo", "Permissões do app", "ir('#/eq-permissoes')")}</div></div>` : ""}
    <div class="grupo-tit">Postos liberados para você</div>
    <div class="secao" style="padding-top:0"><div class="card lista">${(eu.postos || []).map(n => `<div class="linha"><span class="ic">${ICO.loja(20)}</span><div class="meio"><div class="t">${esc(n)}</div></div></div>`).join("")}</div></div>
    <div class="grupo-tit">Avisar no celular</div>
    <div class="secao" style="padding-top:0"><div class="card lista">
      ${eqAprova() ? chave("pedidos", "Pedidos do caixa", esc(meusTipos) + " esperando você", av.pedidos !== false) : ""}
      ${eqPode("ver_alertas") ? chave("tanques", "Tanque baixo e sonda parada", "Os mesmos alertas do Monitor", av.tanques !== false) : ""}
      ${chave("turno", "Abertura e fechamento de turno", "Quem abriu e quem fechou o caixa", !!av.turno)}
    </div></div>
    <div class="grupo-tit">Sessão</div>
    <div class="secao" style="padding-top:0"><div class="card lista">
      ${ir_("perfil", "Usar o app como cliente", "ir('#/inicio')")}
      ${ir_("sair", "Sair do acesso da equipe", "eqConfirmarSaida()")}
    </div></div>
    <div class="vazio" style="text-align:center">Postos SN · versão ${VERSAO_APP}</div>
  </div>`;
}
async function eqAviso(id) {
  const c = document.getElementById("eq-av-" + id);
  if (!c) return;
  const novo = !c.classList.contains("on");
  const r = await apiEq("/app/equipe/api/avisos", { [id]: novo });
  if (!r.ok) { toast(r.erro || "Não consegui salvar."); return; }
  c.classList.toggle("on", novo);
  if (EQ.eu) EQ.eu.avisos = Object.assign({}, EQ.eu.avisos, { [id]: novo });
}
function eqConfirmarSaida() {
  folha(`<h2 style="font-size:1.15rem">Sair do acesso da equipe?</h2>
    <div style="color:var(--mut);margin-top:6px">Os pedidos do caixa deixam de chegar neste celular.</div>
    <button class="btn" onclick="fecharFolha();eqSair()">Sair</button><button class="btn sec" onclick="fecharFolha()">Cancelar</button>`);
}

// ================================================================ PERMISSÕES DO APP (só o dono)
// Por PERFIL, como o resto do sistema (retaguarda › Perfis): o que ligar aqui vale para todo
// mundo daquele perfil. Os postos de cada pessoa continuam no cadastro do operador.
async function telaEqPermissoes(perfil) {
  const eu = await eqEu();
  if (!eu) return;
  if (!eu.master) { ir("#/eq-conta"); return; }
  const r = await apiEq("/app/equipe/api/permissoes");
  const perfis = Array.isArray(r.perfis) ? r.perfis : [];
  const todas = EQ_PERMISSOES.reduce((n, g) => n + g[1].length, 0);
  const ligadas = p => EQ_PERMISSOES.reduce((n, g) => n + g[1].filter(x => (p.pode || {})[x[0]] !== false).length, 0);
  const pf = perfis.find(x => x.id === perfil);
  if (!pf) {
    el().innerHTML = `<div class="tela">${topoSimples("Permissões do app", true)}
      <div class="secao" style="padding-top:2px"><div class="vazio" style="padding-top:0">Escolha o perfil. O que você ligar vale para todo mundo daquele perfil, só nos postos liberados para cada um.</div>
      <div class="card lista">${perfis.map(p => `<button class="linha" onclick="ir('#/eq-permissoes/${esc(p.id)}')"><span class="ic">${ICO.escudo(20)}</span>
        <div class="meio"><div class="t">${esc(p.nome)}</div><div class="s">${num((p.pessoas || []).length)} ${(p.pessoas || []).length === 1 ? "pessoa" : "pessoas"} · ${ligadas(p)} de ${todas} ligadas</div></div>
        <span class="seta">${ICO.seta(18)}</span></button>`).join("")}</div>
      <div class="vazio">O dono (Gerencial) vê e aprova tudo, em todos os postos.</div></div></div>`;
    return;
  }
  el().innerHTML = `<div class="tela">${topoSimples(pf.nome, true)}
    ${EQ_PERMISSOES.map(g => `<div class="grupo-tit" style="padding-top:8px">${g[0]}</div>
      <div class="secao" style="padding-top:0"><div class="card lista">${g[1].map(x => `<button class="linha" onclick="eqPermissao('${esc(pf.id)}','${x[0]}')">
        <div class="meio"><div class="t">${x[1]}</div></div><span class="chave ${(pf.pode || {})[x[0]] !== false ? "on" : ""}" id="eq-pm-${x[0]}"></span></button>`).join("")}</div></div>`).join("")}
    <div class="grupo-tit">Quem tem este perfil</div>
    <div class="secao" style="padding-top:0"><div class="card lista">${(pf.pessoas || []).map(x => `<div class="linha"><span class="ic">${ICO.perfil(20)}</span>
      <div class="meio"><div class="t">${esc(x.nome)}</div><div class="s">${esc((x.postos || []).join(", "))}</div></div></div>`).join("") || '<div class="linha"><div class="meio"><div class="s">Ninguém ainda.</div></div></div>'}</div>
      <div class="vazio">Quem tem o perfil e quais postos cada um enxerga se muda no sistema, em Operadores.</div></div>
  </div>`;
}
async function eqPermissao(perfil, chave) {
  const c = document.getElementById("eq-pm-" + chave);
  if (!c) return;
  const novo = !c.classList.contains("on");
  const r = await apiEq("/app/equipe/api/permissoes", { perfil, chave, ligado: novo });
  if (!r.ok) { toast(r.erro || "Não consegui salvar."); return; }
  c.classList.toggle("on", novo);
}

Object.assign(TELAS, {
  "eq-painel": telaEqPainel, "eq-aprovacoes": telaEqAprovacoes, "eq-pedido": telaEqPedido,
  "eq-postos": telaEqPostos, "eq-posto": telaEqPosto, "eq-conta": telaEqConta, "eq-permissoes": telaEqPermissoes,
});
