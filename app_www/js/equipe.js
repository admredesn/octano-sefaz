// ============================================================
// App Postos SN — ACESSO DA EQUIPE (layout 10/10/2026; servidor 11/10/2026)
// ------------------------------------------------------------
// O dono e os gerentes entram com o mesmo usuário e senha do sistema (retaguarda).
// Decidido pelo Ronan:
//  * 05/10: a primeira versão da equipe são as APROVAÇÕES do caixa (desconto, crédito
//    a prazo, aferição);
//  * 10/10: aprova com UM toque; o pedido VENCE em 1 minuto sem resposta; o gerente vê
//    o mesmo que o dono, mas o dono liga e desliga, por perfil, o que ele pode APROVAR
//    e o que ele pode VER (tela Permissões do app).
// Rotas /app/equipe/api/* = app_equipe.py no servidor. É o SERVIDOR que deixa de mandar
// o que o perfil não pode ver — esconder aqui é só acabamento. As APROVAÇÕES são a etapa
// seguinte: enquanto o servidor disser que ninguém aprova nada (pode.aprovar_* = false),
// a aba e o cartão de pedidos não aparecem.
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
  ["Pode ver", [["ver_vendas", "Vendas do dia em reais"], ["ver_litros", "Litros e número de abastecimentos"], ["ver_tanques", "Nível dos tanques"],
    ["ver_recebido", "Recebido por forma de pagamento"], ["ver_precos", "Preço na bomba"], ["ver_alertas", "Alertas de tanque baixo, sonda parada e posto sem resposta"],
    ["ver_comissao", "Comissão dos vendedores"]]],
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
  // só sai da conta quando o servidor diz que a SESSÃO acabou (17/10/2026: banco lento virava 401 e deslogava a pessoa)
  if (r.status === 401 && tokEq() && /sess/i.test((j && j.erro) || "")) eqSair();
  return Object.assign({ http: r.status }, j);
}

// ---------------------------------------------------------------- telas NA HORA (17/10/2026)
// Ronan: "o app está travando muito, clico em Conta e demora quase 30 s; tem que ser mais fluido, instantâneo". Cada tela
// abre NA HORA com a última resposta guardada (memória + armazenamento do celular) e se atualiza sozinha quando a nova
// chega; sem nada guardado, mostra o esqueleto em vez de ficar parada; sem internet, fica com a última em vez de travar.
const EQ_CACHE = {};
const EQC = "psn_eqc:";
function eqCacheLer(c) {
  if (EQ_CACHE[c]) return EQ_CACHE[c];
  try { const x = JSON.parse(localStorage.getItem(EQC + c) || "null"); if (x && x.r) { EQ_CACHE[c] = x; return x; } } catch (e) { /* sem armazenamento */ }
  return null;
}
function eqCacheGravar(c, r) {
  const x = { t: Date.now(), r };
  EQ_CACHE[c] = x;
  try {
    localStorage.setItem(EQC + c, JSON.stringify(x));
    const idx = JSON.parse(localStorage.getItem("psn_eqc_idx") || "[]").filter(k => k !== c);
    idx.push(c);
    while (idx.length > 30) localStorage.removeItem(EQC + idx.shift());          // guarda as 30 últimas telas
    localStorage.setItem("psn_eqc_idx", JSON.stringify(idx));
  } catch (e) { /* cheio ou sem armazenamento: fica só na memória */ }
}
function eqCacheLimpar() {             // ao sair: o próximo que entrar neste celular não vê os números de quem saiu
  Object.keys(EQ_CACHE).forEach(k => delete EQ_CACHE[k]);
  try {
    JSON.parse(localStorage.getItem("psn_eqc_idx") || "[]").forEach(k => localStorage.removeItem(EQC + k));
    localStorage.removeItem("psn_eqc_idx");
  } catch (e) { /* sem armazenamento */ }
}
// GET com a última resposta guardada: devolve NA HORA e busca a nova por trás; se a nova for diferente e a pessoa ainda
// estiver na mesma tela, `redesenhar()` roda de novo (e aí acha a resposta fresca, sem pedir outra vez).
async function apiEqRapido(caminho, redesenhar, esqueleto) {
  const velho = eqCacheLer(caminho), rota = location.hash;
  if (velho && Date.now() - velho.t < 5000) return velho.r;            // acabou de chegar: não pede de novo
  const buscar = apiEq(caminho).then(r => {
    if (r && (r.ok || Array.isArray(r))) {
      const mudou = !velho || JSON.stringify(r) !== JSON.stringify(velho.r);
      eqCacheGravar(caminho, r);
      if (velho && mudou && location.hash === rota && redesenhar) redesenhar();
    }
    return r;
  });
  if (velho) return velho.r;
  if (esqueleto) esqueleto();
  return buscar;
}
function eqEsqueleto(titulo, voltar) {
  el().innerHTML = `<div class="tela">${titulo ? topoSimples(titulo, voltar) : '<div style="height:calc(env(safe-area-inset-top) + 64px)"></div>'}
    <div class="secao" style="padding-top:2px"><div class="sk" style="height:96px;margin-bottom:12px"></div><div class="sk" style="height:150px;margin-bottom:12px"></div><div class="sk" style="height:150px"></div></div></div>`;
}

function eqSair() {
  eqCacheLimpar();
  if (_eqVigia) { clearInterval(_eqVigia); _eqVigia = null; }
  _eqUltimo = 0;
  if (tokEq()) pushSairEquipe(apiEq);      // o celular para de receber os pedidos (antes de apagar a sessão)
  LS.del("psn_equipe");
  EQ.eu = null;
  if (location.hash.indexOf("#/equipe") !== 0) ir("#/equipe");
}

// ---------------------------------------------------------------- vigia dos pedidos (sem push: o app confere sozinho)
// Enquanto o app está aberto, a cada 6 s pergunta quantos pedidos esperam; chegou pedido novo: vibra, avisa e atualiza a lista.
let _eqVigia = null, _eqUltimo = 0;
function eqVigia() {
  if (_eqVigia || !tokEq() || !eqAprova()) return;
  const tic = async () => {
    if (document.hidden || !tokEq()) return;
    const r = await apiEq("/app/equipe/api/pendentes");
    if (!r || !r.ok) return;
    const n = Number(r.pendentes || 0);
    eqContador(n);
    if (n > _eqUltimo) {
      try { if (navigator.vibrate) navigator.vibrate([220, 120, 220]); } catch (e) { /* ignora */ }
      toast(n === 1 ? "Pedido novo do caixa" : n + " pedidos esperando você");
    }
    const mudou = n !== _eqUltimo;
    _eqUltimo = n;
    if (mudou && /^#\/eq-aprovacoes\/?$/.test(location.hash)) telaEqAprovacoes();
  };
  _eqVigia = setInterval(tic, 6000);
  tic();
}

async function eqEu() {
  if (!EQUIPE_NO_AR || !tokEq()) { ir("#/equipe"); return null; }
  if (EQ.eu) { eqVigia(); return EQ.eu; }
  const guardado = eqCacheLer("/app/equipe/api/eu");
  if (guardado) {                                          // abre na hora; nome, postos e permissões novos chegam por trás
    EQ.eu = guardado.r;
    eqBarraPermissoes();
    eqVigia();
    eqEuAtualizar();
    return EQ.eu;
  }
  const r = await apiEq("/app/equipe/api/eu");
  if (!r.ok) { eqSair(); return null; }
  eqEuAplicar(r);
  return r;
}
function eqEuAplicar(r) {
  EQ.eu = r;
  eqCacheGravar("/app/equipe/api/eu", r);
  eqBarraPermissoes();
  eqVigia();
  if (r.push && eqAprova()) pushRegistrarEquipe(apiEq);    // app das lojas: pedido chega com o app fechado
}
async function eqEuAtualizar() {
  const r = await apiEq("/app/equipe/api/eu");
  if (r && r.ok) eqEuAplicar(r);                           // sessão encerrada (401): o apiEq já tira
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
  // telas de dentro acendem a aba de onde se chega nelas (Gestão, 17/10/2026: Postos, comparativo, estoque e comissão)
  const ativo = { "eq-pedido": "eq-aprovacoes", "eq-posto": "eq-gestao", "eq-postos": "eq-gestao", "eq-comparativo": "eq-gestao",
    "eq-estoque": "eq-gestao", "eq-comissao": "eq-gestao", "eq-descargas": "eq-gestao", "eq-permissoes": "eq-conta" }[nome] || nome;
  b.querySelectorAll("[data-aba]").forEach(x => x.classList.toggle("on", x.dataset.aba === ativo));
}
function eqBarraPermissoes() {       // quem não aprova nada não tem a aba Aprovações; quem não vê vendas não tem a aba Vendas…
  const b = document.getElementById("barra-eq");
  if (!b) return;
  const some = { "eq-aprovacoes": !eqAprova(), "eq-vendas": !(eqPode("ver_vendas") || eqPode("ver_litros")),
    "eq-gestao": !(eqVePostos() || eqPode("ver_comissao")) };
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
    <div style="text-align:center;color:var(--mut);font-size:.9rem;margin-bottom:8px">Entre com o mesmo usuário e senha do sistema.</div>
    <div class="campo"><label for="g-email">Usuário</label><input id="g-email" type="text" autocomplete="username" autocapitalize="none" autocorrect="off" spellcheck="false" placeholder="seu usuário ou e-mail"></div>
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
  if (eqPode("ver_litros")) k.push(["Litros", num(Math.round(p.litros || 0)) + " L"], p.abastecimentos != null ? ["Abastec.", num(p.abastecimentos)] : ["Cupons", num(p.cupons)]);
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
  const r = await apiEqRapido("/app/equipe/api/painel", telaEqPainel, () => eqEsqueleto());
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
// APROVAR EM LOTE (17/10/2026, pedido do Ronan com 6 aferições do Miranda na fila): "Selecionar" marca os pedidos (ou
// "Todos") e aprova/recusa de uma vez, com UMA confirmação. Cada pedido passa pela MESMA rota de um a um (/decidir):
// o servidor confere permissão, posto e validade de cada um — o lote não pula regra nenhuma.
const EQA = { selecionando: false, marcados: new Set(), lista: [], ocupado: false };

async function telaEqAprovacoes(aba) {
  const eu = await eqEu();
  if (!eu) return;
  if (EQA.ocupado) return;                         // aprovando em lote: a vigia não redesenha no meio
  const resolvidas = aba === "resolvidas";
  const r = await apiEqRapido("/app/equipe/api/aprovacoes?situacao=" + (resolvidas ? "resolvidas" : "pendentes"), () => telaEqAprovacoes(aba),
    () => eqEsqueleto("Aprovações"));
  const lst = Array.isArray(r) ? r : [];
  if (!resolvidas) {
    eqContador(lst.length);
    EQA.lista = lst;
    EQA.marcados = new Set([...EQA.marcados].filter(id => lst.some(x => x.id === id)));     // some o que já foi resolvido
    if (lst.length < 2) EQA.selecionando = false;
  } else EQA.selecionando = false;
  eqaDesenhar(lst, resolvidas);
}
// desenha a lista sem ir ao servidor (marcar/desmarcar fica instantâneo no celular)
function eqaDesenhar(lst, resolvidas) {
  const sel = EQA.selecionando && !resolvidas;
  const ST = { aprovado: ["Aprovado", "verde"], recusado: ["Recusado", "cinza"], expirado: ["Venceu", "ouro"] };
  const linha = p => {
    const t = EQ_TIPOS[p.tipo] || { nome: p.tipo, ico: "aprovar" }, st = ST[p.situacao];
    const quando = !st && p.vence_em ? `<b data-vence="${esc(p.vence_em)}" style="color:#8A5A00"></b>` : esc(haQuanto(p.quando));
    const marcado = sel && EQA.marcados.has(p.id);
    return `<button class="linha ${marcado ? "marcada" : ""}" onclick="${sel ? `eqaMarcar('${esc(p.id)}')` : `ir('#/eq-pedido/${esc(p.id)}')`}"><span class="ic">${ICO[t.ico](20)}</span>
      <div class="meio"><div class="t">${esc((p.info && p.titulo_curto) || p.titulo || t.nome)}</div>${p.info ? `<div class="s" style="color:var(--txt);font-weight:600;font-variant-numeric:tabular-nums">${esc(p.info)}</div>` : ""}<div class="s">${esc(p.posto)} · ${esc(primeiroNome(p.pediu))} · ${quando}</div></div>
      ${sel ? `<span class="sel-marca ${marcado ? "on" : ""}">${marcado ? ICO.check(16) : ""}</span>` : st ? `<span class="tag ${st[1]}">${st[0]}</span>` : `<span class="seta">${ICO.seta(18)}</span>`}</button>`;
  };
  const n = EQA.marcados.size, todos = n === lst.length && n > 0;
  el().innerHTML = `<div class="tela">${topoSimples("Aprovações", false)}
    <div class="secao" style="padding-top:2px">
      <div class="seg"><button class="${resolvidas ? "" : "on"}" onclick="ir('#/eq-aprovacoes')">Pendentes</button><button class="${resolvidas ? "on" : ""}" onclick="ir('#/eq-aprovacoes/resolvidas')">Resolvidas</button></div>
    </div>
    ${!resolvidas && lst.length >= 2 ? `<div class="sel-topo">${sel
      ? `<button class="link" onclick="eqaTodos()">${todos ? "Desmarcar todos" : "Selecionar todos (" + lst.length + ")"}</button><button class="link" onclick="eqaSelecionar(false)">Cancelar</button>`
      : `<span>${lst.length} pedidos esperando</span><button class="link" onclick="eqaSelecionar(true)">Selecionar</button>`}</div>` : ""}
    <div class="secao" style="${!resolvidas && lst.length >= 2 ? "padding-top:8px" : ""}">${lst.length ? `<div class="card lista">${lst.map(linha).join("")}</div>`
      : `<div class="card vazio" style="padding:16px">${resolvidas ? "Nenhum pedido resolvido ainda." : "Nenhum pedido esperando. Quando o caixa precisar de você, o celular avisa."}</div>`}
      ${resolvidas || !lst.some(x => x.vence_em) ? "" : '<div class="vazio" style="text-align:center">O caixa espera a resposta por 1 minuto. Depois disso o pedido vence e ele precisa pedir de novo.</div>'}</div>
    ${sel ? `<div class="lote"><button class="btn recusa" ${n ? "" : "disabled"} onclick="eqaLote(false)">Recusar</button>
      <button class="btn verde" ${n ? "" : "disabled"} onclick="eqaLote(true)">${n ? "Aprovar " + n : "Marque os pedidos"}</button></div><div style="height:84px"></div>` : ""}
  </div>`;
  if (!resolvidas && lst.length) eqRelogio();
}
function eqaSelecionar(on) { EQA.selecionando = on; EQA.marcados = new Set(); eqaDesenhar(EQA.lista, false); }
function eqaMarcar(id) { if (EQA.marcados.has(id)) EQA.marcados.delete(id); else EQA.marcados.add(id); eqaDesenhar(EQA.lista, false); }
function eqaTodos() {
  EQA.marcados = EQA.marcados.size === EQA.lista.length ? new Set() : new Set(EQA.lista.map(x => x.id));
  eqaDesenhar(EQA.lista, false);
}
// resumo do que vai ser decidido: "6 aferições · Posto Miranda"
function eqaResumo(ids) {
  const it = EQA.lista.filter(x => ids.includes(x.id)), tipos = {}, postos = new Set(it.map(x => x.posto));
  it.forEach(x => { const nome = { afericao: "aferição", desconto: "desconto", prazo: "a prazo" }[x.tipo] || "pedido"; tipos[nome] = (tipos[nome] || 0) + 1; });
  const plural = (q, s) => q + " " + (q === 1 ? s : s === "aferição" ? "aferições" : s === "desconto" ? "descontos" : s === "a prazo" ? "a prazo" : s + "s");
  return Object.keys(tipos).map(k => plural(tipos[k], k)).join(", ") + " · " + [...postos].join(", ");
}
function eqaLote(aprovar) {
  const ids = [...EQA.marcados];
  if (!ids.length) return;
  folha(`<h2 style="font-size:1.15rem">${aprovar ? "Aprovar" : "Recusar"} ${ids.length} ${ids.length === 1 ? "pedido" : "pedidos"}?</h2>
    <div style="color:var(--mut);margin-top:6px">${esc(eqaResumo(ids))}. ${aprovar ? "O caixa recebe a resposta na hora." : "O caixa recebe a recusa na hora."}</div>
    ${aprovar ? "" : '<div class="campo"><label for="lt-motivo">Motivo (opcional, vale para todos)</label><input id="lt-motivo" maxlength="120" placeholder="Ex.: aferição repetida"></div>'}
    <button class="btn ${aprovar ? "verde" : "verm"}" id="lt-ok">${aprovar ? "Aprovar" : "Recusar"} ${ids.length}</button><button class="btn sec" onclick="fecharFolha()">Voltar</button>`);
  document.getElementById("lt-ok").onclick = async () => {
    const motivo = aprovar ? "" : (document.getElementById("lt-motivo").value || "").trim();
    const btn = document.getElementById("lt-ok");
    btn.disabled = true;
    EQA.ocupado = true;
    let ok = 0;
    const falhas = [];
    try {
      for (let i = 0; i < ids.length; i++) {
        btn.textContent = (aprovar ? "Aprovando " : "Recusando ") + (i + 1) + " de " + ids.length + "…";
        const r = await apiEq("/app/equipe/api/aprovacoes/" + encodeURIComponent(ids[i]) + "/decidir", { aprovar, motivo });
        if (r && r.ok) ok++; else falhas.push(r && r.erro ? r.erro : "não consegui registrar");
      }
    } finally {
      EQA.ocupado = false;
    }
    fecharFolha();
    EQA.selecionando = false;
    EQA.marcados = new Set();
    toast(ok ? `${ok} ${ok === 1 ? "pedido" : "pedidos"} ${aprovar ? (ok === 1 ? "aprovado" : "aprovados") : (ok === 1 ? "recusado" : "recusados")}.` +
      (falhas.length ? ` ${falhas.length} não ${falhas.length === 1 ? "foi" : "foram"}: ${falhas[0]}` : "") : "Nenhum foi registrado: " + (falhas[0] || "tente de novo."), 4200);
    telaEqAprovacoes();
  };
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
  const comPrazo = !!p.vence_em;         // aferição não vence em 1 minuto: fica esperando até alguém decidir
  const total = Math.max(1, Math.round((new Date(p.vence_em).getTime() - new Date(p.quando).getTime()) / 1000) || 60);
  el().innerHTML = `<div class="tela">${topoSimples(t.nome, true)}
    <div class="secao" style="padding-top:2px"><div class="card resumo">
      ${pendente ? (comPrazo ? `<span class="tag ouro" id="pd-selo">Esperando você · <span data-vence="${esc(p.vence_em)}"></span></span>
        <div class="tempo"><i data-vence="${esc(p.vence_em)}" data-barra="${total}"></i></div>` : `<span class="tag ouro" id="pd-selo">Esperando você</span>`) : `<span class="tag ${SELO[1]}">${esc(SELO[0])}</span>`}
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
  if (!comPrazo) return;                                                     // sem prazo (aferição): não há relógio nem vencimento
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
  const r = await apiEqRapido("/app/equipe/api/painel", telaEqPostos, () => eqEsqueleto("Postos"));
  const postos = Array.isArray(r.postos) ? r.postos : [];
  el().innerHTML = `<div class="tela">${topoSimples("Postos", false)}
    <div class="secao" style="padding-top:2px">${postos.length ? eqTotalRede(postos) + postos.map(eqCartaoPosto).join("")
      : '<div class="card vazio" style="padding:16px">Nenhum posto liberado para o seu perfil.</div>'}</div></div>`;
}

async function telaEqPosto(id) {
  const eu = await eqEu();
  if (!eu) return;
  const p = await apiEqRapido("/app/equipe/api/posto/" + encodeURIComponent(id), () => telaEqPosto(id), () => eqEsqueleto("Posto", true));
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
    ${formas.length ? `<div class="secao"><div class="secao-tit"><h2>Recebido hoje</h2></div><div class="card">${formas.map(f => `<div class="par"><span>${esc(f[0])}</span><b>${brl(f[1])}</b></div>`).join("")}${
        p.formas_falta ? `<div class="par"><span style="color:var(--mut)">Sem forma de pagamento ainda</span><b style="color:var(--mut);white-space:nowrap">${brl(p.formas_falta)}</b></div>` : ""}</div></div>` : ""}
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
    </div>${eu.push === false ? '<div class="vazio">Os avisos no celular começam quando o app estiver nas lojas (Apple e Google). Até lá fica guardado o que você quer receber.</div>'
      : !NATIVO ? '<div class="vazio">O aviso na tela do celular chega pelo app Postos SN instalado da loja (App Store ou Google Play). Aqui no navegador, os pedidos aparecem com esta página aberta.</div>' : ""}</div>
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
  const r = await apiEqRapido("/app/equipe/api/permissoes", () => telaEqPermissoes(perfil), () => eqEsqueleto("Permissões do app", true));
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
        <div class="meio"><div class="t">${x[1]}</div></div><span class="chave ${(pf.pode || {})[x[0]] !== false ? "on" : ""}" id="eq-pm-${x[0]}"></span></button>`).join("")}</div>${
        g[0] === "Pode aprovar" && Object.values(r.aprovacoes_tipos || {}).some(v => !v) ? '<div class="vazio">' + (r.aprovacoes_no_ar ? "Já chegam no celular: <b>" + Object.keys(r.aprovacoes_tipos || {}).filter(k => r.aprovacoes_tipos[k]).map(k => ({ afericao: "Aferição", desconto: "Desconto no caixa", prazo: "Venda a prazo" })[k] || k).join(" e ") + "</b>. " : "") + 'O que ainda não chega entra na próxima etapa: o que você deixar ligado aqui já fica pronto.</div>' : ""}</div>`).join("")}
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
