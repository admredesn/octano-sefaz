// ============================================================
// App Postos SN — telas do CLIENTE e roteador (05/10/2026)
// ------------------------------------------------------------
// Referência visual: o app da Rede Aliança que o Ronan mandou (barra Início · Resgatar ·
// bomba · Cupons · Perfil). Benefício = OS DOIS: cashback no Pix + pontos (1 por R$ 1,
// valem 6 meses) trocados por prêmios no posto com código conferido no caixa.
// Tudo que vem do servidor passa por esc() antes de virar HTML.
// ============================================================

const PUBLICAS = ["entrar", "cadastro", "confirmar", "esqueci", "equipe"];
const COM_BARRA = ["inicio", "resgatar", "premios", "cupons", "perfil", "historico", "extrato", "parceiros",
  "postos", "favorito", "notificacoes", "dados", "oferta", "acionar"];
const CATEGORIAS = [
  ["troca_oleo", "Troca de óleo", "oleo"], ["servicos", "Serviços", "servico"],
  ["conveniencia", "Conveniência", "loja"], ["aditivos", "Aditivos", "aditivo"],
];

function el() { return document.getElementById("tela"); }
function ir(h) { location.hash = h; }

function barra(nome) {
  const b = document.getElementById("barra");
  const mostra = COM_BARRA.includes(nome) && !!tok();
  b.style.display = mostra ? "" : "none";
  document.querySelector(".app").classList.toggle("sem-barra", !mostra);
  const ativo = { premios: "resgatar", postos: "resgatar", historico: "perfil", extrato: "inicio", parceiros: "perfil",
    favorito: "perfil", dados: "perfil", notificacoes: "inicio", oferta: "inicio", acionar: "" }[nome] || nome;
  b.querySelectorAll("[data-aba]").forEach(x => x.classList.toggle("on", x.dataset.aba === ativo));
  if (typeof barraEquipe === "function") barraEquipe(nome);     // a equipe tem a barra dela (equipe.js)
}

async function rota() {
  const h = (location.hash || "").replace(/^#\/?/, "") || (tok() ? "inicio" : "entrar");
  const [nome, arg] = h.split("/");
  if (!tok() && !PUBLICAS.includes(nome)) { ir("#/entrar"); return; }
  if (tok() && (nome === "entrar" || nome === "cadastro")) { ir("#/inicio"); return; }
  if (typeof pararCamera === "function") pararCamera();
  if (typeof pararVivo === "function") pararVivo();
  fecharFolha();
  barra(nome);
  const fn = TELAS[nome] || TELAS.inicio;
  try { await fn(decodeURIComponent(arg || "")); }
  catch (e) { el().innerHTML = `<div class="secao"><div class="card" style="padding:18px">Algo deu errado ao abrir esta tela.<div class="msg erro">${esc(e.message || e)}</div><button class="btn" onclick="rota()">Tentar de novo</button></div></div>`; }
  window.scrollTo(0, 0);
}

function topoSimples(titulo, voltar) {
  return `<div class="topo-simples">${voltar ? `<button class="voltar" onclick="history.back()" aria-label="Voltar">${ICO.voltar()}</button>` : ""}<h1>${esc(titulo)}</h1></div>`;
}

// ---------------------------------------------------------------- dados em cache
async function carregarMe(forcar) {
  if (S.me && !forcar) return S.me;
  const r = await api("/cashback/api/me");
  if (r.ok) S.me = r;
  return S.me;
}
async function carregarPostos() {
  if (S.postos) return S.postos;
  const r = await api("/cashback/api/rede/postos");
  S.postos = Array.isArray(r) ? r : [];
  return S.postos;
}
function nomePosto(id) { const p = (S.postos || []).find(x => x.id === id); return p ? p.nome : ""; }

// ================================================================ ENTRAR
function telaEntrar() {
  el().innerHTML = `<div class="entrar tela">
    <img class="marca" src="${LOGO_AZUL}" alt="Rede SN">
    <div class="slogan">Mais que combustível, confiança.</div>
    <div class="campo"><label for="e-cpf">CPF</label><input id="e-cpf" inputmode="numeric" autocomplete="username" placeholder="000.000.000-00"></div>
    <div class="campo"><label for="e-senha">Senha</label><input id="e-senha" type="password" autocomplete="current-password"></div>
    <button class="btn" id="e-btn">Entrar</button>
    <button class="btn sec" onclick="ir('#/cadastro')">Criar minha conta</button>
    <div class="msg" id="e-msg"></div>
    <a class="link" href="#/esqueci" style="display:block;text-align:center;margin-top:12px">Esqueci minha senha</a>
    ${EQUIPE_NO_AR ? '<div class="equipe-link">É da equipe de um posto? <a class="link" href="#/equipe">Acesso da equipe</a></div>' : ""}
  </div>`;
  mascaraCpf(document.getElementById("e-cpf"));
  const entrar = async () => {
    const m = document.getElementById("e-msg");
    m.className = "msg"; m.textContent = "Entrando…";
    const cpf = document.getElementById("e-cpf").value;
    const r = await api("/cashback/api/login", { cpf, senha: document.getElementById("e-senha").value });
    if (r.token) { LS.set("psn_token", r.token); S.me = null; ir("#/inicio"); return; }
    if (r.verificar) { S.conf = { cpf, novo: false, r }; ir("#/confirmar"); return; }
    m.className = "msg erro"; m.textContent = r.erro || "Não consegui entrar.";
  };
  document.getElementById("e-btn").onclick = entrar;
  document.getElementById("e-senha").addEventListener("keydown", e => { if (e.key === "Enter") entrar(); });
}

function telaCadastro() {
  el().innerHTML = `<div class="tela">${topoSimples("Criar minha conta", true)}
    <div class="form">
      <div class="campo"><label>Nome completo</label><input id="c-nome" autocomplete="name" placeholder="Como no documento"></div>
      <div class="campo"><label>CPF</label><input id="c-cpf" inputmode="numeric" placeholder="000.000.000-00"></div>
      <div class="campo"><label>Celular (WhatsApp)</label><input id="c-tel" inputmode="numeric" autocomplete="tel" placeholder="(31) 99999-9999"></div>
      <div class="campo"><label>E-mail (opcional)</label><input id="c-email" type="email" autocomplete="email"></div>
      <div class="campo"><label>Chave Pix (onde cai o cashback)</label><input id="c-pix" placeholder="CPF, celular, e-mail ou chave aleatória"></div>
      <div class="duas">
        <div class="campo"><label>Senha</label><input id="c-senha" type="password" autocomplete="new-password" placeholder="mín. 6"></div>
        <div class="campo"><label>Repita a senha</label><input id="c-senha2" type="password" autocomplete="new-password"></div>
      </div>
      <label class="check"><input type="checkbox" id="c-termos"> <span>Li e aceito os termos de uso e a política de privacidade da Rede SN.</span></label>
      <label class="check"><input type="checkbox" id="c-mkt"> <span>Quero receber ofertas e promoções dos Postos SN (dá para mudar depois no Perfil).</span></label>
      <button class="btn" id="c-btn">Criar conta</button>
      <div class="msg" id="c-msg"></div>
      <div class="vazio" style="text-align:center">Vamos mandar um código para o WhatsApp que o posto tem de você, para confirmar que é você mesmo.</div>
    </div></div>`;
  mascaraCpf(document.getElementById("c-cpf"));
  mascaraFone(document.getElementById("c-tel"));
  document.getElementById("c-btn").onclick = async () => {
    const m = document.getElementById("c-msg"), v = id => document.getElementById(id).value;
    m.className = "msg erro";
    if (v("c-senha") !== v("c-senha2")) { m.textContent = "As senhas não conferem."; return; }
    if (!document.getElementById("c-termos").checked) { m.textContent = "Para criar a conta, aceite os termos e a política de privacidade."; return; }
    m.className = "msg"; m.textContent = "Criando a conta…";
    const r = await api("/cashback/api/cadastro", {
      nome: v("c-nome"), cpf: v("c-cpf"), telefone: v("c-tel"), email: v("c-email"), chave_pix: v("c-pix"),
      senha: v("c-senha"), posto: S.posto || null,
    });
    if (r.verificar) { S.conf = { cpf: v("c-cpf"), novo: true, mkt: document.getElementById("c-mkt").checked, r }; ir("#/confirmar"); return; }
    m.className = "msg erro"; m.textContent = r.erro || "Não consegui criar a conta.";
  };
}

function telaConfirmar() {
  const c = S.conf;
  if (!c) { ir("#/entrar"); return; }
  const r = c.r || {};
  el().innerHTML = `<div class="tela">${topoSimples("Confirme que é você", true)}
    <div class="form">
      <div style="color:var(--mut)">${r.destino ? "Enviamos um código de 6 dígitos para o <b>" + esc(r.destino) + "</b>" + (r.contato_do_posto ? " — o contato que o posto tem de você." : ".") : "Precisamos confirmar que é você antes de entrar."}</div>
      <div class="campo"><label>Código</label><input id="k-cod" inputmode="numeric" maxlength="6" autocomplete="one-time-code" style="font-size:1.6rem;letter-spacing:.4em;text-align:center"></div>
      <button class="btn" id="k-btn">Confirmar</button>
      <button class="btn sec" id="k-re">Reenviar o código</button>
      <div class="msg ${r.erro_envio ? "erro" : ""}" id="k-msg">${esc(r.erro_envio || "")}</div>
    </div></div>`;
  document.getElementById("k-cod").focus();
  document.getElementById("k-btn").onclick = async () => {
    const m = document.getElementById("k-msg");
    m.className = "msg"; m.textContent = "Conferindo…";
    const x = await api("/cashback/api/cadastro/confirmar", { cpf: c.cpf, codigo: document.getElementById("k-cod").value });
    if (!x.token) { m.className = "msg erro"; m.textContent = x.erro || "Não consegui confirmar."; return; }
    LS.set("psn_token", x.token);
    if (c.mkt) await api("/cashback/api/perfil", { aceita_marketing: true });
    S.conf = null; S.me = null;
    toast(c.novo ? "Conta criada! Bem-vindo aos Postos SN." : "Pronto, conta confirmada.");
    ir("#/inicio");
  };
  document.getElementById("k-re").onclick = async () => {
    const m = document.getElementById("k-msg");
    const x = await api("/cashback/api/cadastro/reenviar", { cpf: c.cpf });
    m.className = x.ok ? "msg ok" : "msg erro";
    m.textContent = x.ok ? (x.destino ? "Código reenviado para " + x.destino + "." : "Se o código não chegar, peça ajuda no caixa do posto.") : (x.erro || "Falha no envio.");
  };
}

function telaEsqueci() {
  el().innerHTML = `<div class="tela">${topoSimples("Recuperar senha", true)}
    <div class="form">
      <div style="color:var(--mut)">Mandamos um código para o WhatsApp que o posto tem de você.</div>
      <div class="campo"><label>CPF</label><input id="q-cpf" inputmode="numeric" placeholder="000.000.000-00"></div>
      <button class="btn" id="q-pedir">Enviar o código</button>
      <div id="q-passo2" style="display:none">
        <div class="campo"><label>Código</label><input id="q-cod" inputmode="numeric" maxlength="6" autocomplete="one-time-code"></div>
        <div class="duas">
          <div class="campo"><label>Nova senha</label><input id="q-s1" type="password" autocomplete="new-password"></div>
          <div class="campo"><label>Repita</label><input id="q-s2" type="password" autocomplete="new-password"></div>
        </div>
        <button class="btn" id="q-trocar">Salvar nova senha</button>
      </div>
      <div class="msg" id="q-msg"></div>
    </div></div>`;
  mascaraCpf(document.getElementById("q-cpf"));
  const m = () => document.getElementById("q-msg");
  document.getElementById("q-pedir").onclick = async () => {
    m().className = "msg"; m().textContent = "Enviando…";
    const r = await api("/cashback/api/senha/pedir", { cpf: document.getElementById("q-cpf").value, canal: "whatsapp" });
    if (!r.ok) { m().className = "msg erro"; m().textContent = r.erro || "Falha no envio."; return; }
    document.getElementById("q-passo2").style.display = "";
    m().className = "msg ok"; m().textContent = r.destino ? "Código enviado para " + r.destino + "." : (r.aviso || "");
  };
  document.getElementById("q-trocar").onclick = async () => {
    const s1 = document.getElementById("q-s1").value;
    if (s1 !== document.getElementById("q-s2").value) { m().className = "msg erro"; m().textContent = "As senhas não conferem."; return; }
    const r = await api("/cashback/api/senha/trocar", { cpf: document.getElementById("q-cpf").value, codigo: document.getElementById("q-cod").value, senha: s1 });
    if (r.token) { LS.set("psn_token", r.token); S.me = null; toast("Senha alterada."); ir("#/inicio"); return; }
    m().className = "msg erro"; m().textContent = r.erro || "Não consegui trocar a senha.";
  };
}

// O acesso da equipe (entrar, painel, aprovações, postos, conta) fica em equipe.js.

// ================================================================ INÍCIO
async function telaInicio() {
  const [me, pts, ofertas, postos, hist] = await Promise.all([
    carregarMe(true), api("/cashback/api/pontos"), api("/cashback/api/ofertas?posto=" + encodeURIComponent(S.posto || "")),
    carregarPostos(), api("/cashback/api/historico"),
  ]);
  if (!me) { sessaoCaiu(); return; }
  S.pontos = pts.ok ? pts : null;
  S.ofertas = Array.isArray(ofertas) ? ofertas : [];
  const hora = new Date().getHours();
  const ola = hora < 12 ? "Bom dia," : hora < 18 ? "Boa tarde," : "Boa noite,";
  const banners = S.ofertas.filter(o => o.destaque);
  const compras = (Array.isArray(hist) ? hist : []).slice(0, 3);
  const meus = [...new Set((Array.isArray(hist) ? hist : []).map(v => v.empresa_id))].map(id => postos.find(p => p.id === id)).filter(Boolean);
  const listaPostos = meus.length ? meus : postos;
  el().innerHTML = `<div class="tela">
    <div class="topo-azul"><div class="topo-linha">
      <div class="topo-logo"><img src="${LOGO_AZUL}" alt="Rede SN"></div>
      <div><div class="topo-ola">${ola}</div><div class="topo-nome">${esc(primeiroNome(me.nome))}</div></div>
      <button class="sino" onclick="ir('#/notificacoes')" aria-label="Notificações">${ICO.sino(22)}</button>
    </div></div>
    <div class="saldo">
      <div><div class="rot">Pontos</div><div class="val pts">${num(S.pontos ? S.pontos.saldo : 0)}</div></div>
      <div><div class="rot">Cashback recebido</div><div class="val cb">${brl(me.total_pago)}</div></div>
      <button class="btn-borda" onclick="ir('#/extrato')">Extrato</button>
    </div>
    ${S.pontos && S.pontos.a_vencer_30d > 0 ? `<div class="secao"><div class="card" style="padding:12px 14px;background:var(--ouro-fundo);box-shadow:none;font-size:.86rem">⏳ <b>${num(S.pontos.a_vencer_30d)} ponto(s)</b> vencem nos próximos 30 dias. <a class="link" href="#/resgatar">Trocar agora</a></div></div>` : ""}
    <div class="secao"><div class="secao-tit"><h2>Categorias</h2></div>
      <div class="categorias">${CATEGORIAS.map(c => `<button class="cat" onclick="S.cat='${c[0]}';ir('#/resgatar')"><span class="ic">${ICO[c[2]](22)}</span>${c[1]}</button>`).join("")}</div>
    </div>
    ${banners.length ? `<div class="secao"><div class="carrossel" id="carrossel">${banners.map(o => `
      <button class="banner ${o.imagem_url ? "" : "sem-foto"}" onclick="ir('#/oferta/${esc(o.id)}')">
        ${o.imagem_url ? `<img src="${esc(o.imagem_url)}" alt="">` : ""}
        <div class="legenda"><b>${esc(o.titulo)}</b><span>${esc(o.texto || "")}</span></div></button>`).join("")}</div>
      ${banners.length > 1 ? `<div class="pontinhos" id="pontinhos">${banners.map((b, i) => `<i class="${i ? "" : "on"}"></i>`).join("")}</div>` : ""}</div>` : ""}
    <div class="secao"><div class="secao-tit"><h2>${meus.length ? "Seus postos" : "Postos da rede"}</h2><a href="#/postos">Ver todos</a></div>
      <div class="postos-h">${listaPostos.map(p => `<button class="posto-mini" onclick="ir('#/premios/${esc(p.id)}')">
        <div class="foto" ${p.foto_url ? `style="background-image:url('${esc(p.foto_url)}')"` : ""}>${p.foto_url ? "" : ICO.bomba(30)}</div>
        <div class="n">${esc(p.nome)}</div><div class="c">${esc(p.cidade || "")}</div></button>`).join("")}</div>
    </div>
    <div class="secao"><div class="secao-tit"><h2>Últimas compras</h2>${compras.length ? '<a href="#/historico">Ver todas</a>' : ""}</div>
      ${compras.length ? `<div class="card lista">${compras.map(linhaCompra).join("")}</div>`
        : `<div class="card vazio" style="padding:16px">Informe o seu CPF no caixa ou toque no botão da bomba antes de abastecer: a compra aparece aqui e você ganha <b>1 ponto a cada R$ 1</b>.</div>`}
    </div>
  </div>`;
  const car = document.getElementById("carrossel"), pon = document.getElementById("pontinhos");
  if (car && pon) car.addEventListener("scroll", () => {
    const i = Math.round(car.scrollLeft / (car.scrollWidth / banners.length));
    pon.querySelectorAll("i").forEach((x, j) => x.classList.toggle("on", j === i));
  }, { passive: true });
}

function linhaCompra(v) {
  const comb = (v.itens || []).filter(i => i.tipo === "abastecimento");
  const desc = comb.length ? comb.map(i => `${Number(i.qtd || 0).toLocaleString("pt-BR", { maximumFractionDigits: 2 })} L ${String(i.desc || "").toLowerCase()}`).join(" + ")
    : (v.itens || []).map(i => String(i.desc || "").toLowerCase()).slice(0, 2).join(", ");
  return `<div class="linha"><span class="ic">${ICO[comb.length ? "bomba" : "loja"](20)}</span>
    <div class="meio"><div class="t">${esc(v.posto || "Posto")}</div><div class="s">${esc(desc)} · ${esc(dataBr(v.quando))}</div></div>
    <div class="dir">${brl(v.valor)}<div class="s" style="color:var(--ouro);font-weight:700">+${num(Math.floor(v.valor || 0))} pts</div></div></div>`;
}

// ================================================================ RESGATAR (postos) e POSTOS
async function telaResgatar(modo) {
  const postos = await carregarPostos();
  const titulo = modo === "postos" ? "Postos da rede" : "Selecione um local";
  el().innerHTML = `<div class="tela">${topoSimples(titulo, modo === "postos")}
    <div class="secao" style="padding-top:4px">
      <input class="busca" id="r-busca" placeholder="Pesquisar">
      <div style="font-size:.82rem;color:var(--mut);margin-bottom:6px">Ordenar por:</div>
      <div class="seg"><button id="r-dist">Distância</button><button id="r-nome" class="on">Nome</button></div>
      ${S.cat ? `<div style="margin-top:10px"><span class="tag ouro">${esc((CATEGORIAS.find(c => c[0] === S.cat) || [0, ""])[1])}</span> <a class="link" href="javascript:void(0)" onclick="S.cat='';rota()">limpar</a></div>` : ""}
      <div id="r-lista" style="margin-top:12px"></div>
    </div></div>`;
  let ordem = "nome", aqui = null;
  const desenhar = () => {
    const q = document.getElementById("r-busca").value.trim().toLowerCase();
    let lst = postos.filter(p => !q || (p.nome + " " + (p.cidade || "") + " " + (p.endereco || "")).toLowerCase().includes(q))
      .map(p => ({ ...p, km: aqui && p.latitude != null && p.longitude != null ? distKm(aqui, { lat: +p.latitude, lon: +p.longitude }) : null }));
    lst.sort((a, b) => ordem === "dist" ? ((a.km == null) - (b.km == null) || (a.km || 0) - (b.km || 0) || a.nome.localeCompare(b.nome)) : a.nome.localeCompare(b.nome));
    document.getElementById("r-lista").innerHTML = lst.length ? `<div class="card lista">${lst.map(p => `
      <button class="linha" onclick="ir('#/premios/${esc(p.id)}')">
        <span class="ic">${ICO.loja(20)}</span>
        <div class="meio"><div class="t">${esc(p.nome)}</div><div class="s">${esc([p.endereco, p.cidade].filter(Boolean).join(" · "))}</div>
          ${p.km != null ? `<div class="s">${ICO.local(13)} ${p.km.toLocaleString("pt-BR", { maximumFractionDigits: 1 })} km</div>` : ""}</div>
        <span class="link">Ver produtos</span></button>`).join("")}</div>` : '<div class="vazio">Nenhum posto encontrado.</div>';
  };
  document.getElementById("r-busca").addEventListener("input", desenhar);
  document.getElementById("r-nome").onclick = () => { ordem = "nome"; marcar(); desenhar(); };
  document.getElementById("r-dist").onclick = () => {
    ordem = "dist"; marcar();
    if (!navigator.geolocation) { toast("Este aparelho não informa a localização."); return; }
    navigator.geolocation.getCurrentPosition(pos => {
      aqui = { lat: pos.coords.latitude, lon: pos.coords.longitude };
      if (!postos.some(p => p.latitude != null)) toast("A distância aparece quando os postos tiverem a localização cadastrada.");
      desenhar();
    }, () => toast("Sem permissão de localização: ordenei por nome."), { timeout: 8000 });
  };
  const marcar = () => {
    document.getElementById("r-dist").classList.toggle("on", ordem === "dist");
    document.getElementById("r-nome").classList.toggle("on", ordem === "nome");
  };
  desenhar();
}

// ================================================================ PRÊMIOS de um posto
async function telaPremios(posto) {
  if (!posto) { ir("#/resgatar"); return; }
  await carregarPostos();
  const [lst, pts] = await Promise.all([api("/cashback/api/premios?posto=" + encodeURIComponent(posto)), api("/cashback/api/pontos")]);
  S.pontos = pts.ok ? pts : S.pontos;
  const saldo = S.pontos ? S.pontos.saldo : 0;
  const todos = Array.isArray(lst) ? lst : [];
  el().innerHTML = `<div class="tela">${topoSimples("Selecione um produto", true)}
    <div class="secao" style="padding-top:2px">
      <div style="color:var(--mut);font-size:.86rem;margin:-4px 0 10px">${esc(nomePosto(posto))} · você tem <b style="color:var(--ouro)">${num(saldo)} pontos</b></div>
      <input class="busca" id="p-busca" placeholder="Pesquisar">
      <div style="font-size:.82rem;color:var(--mut);margin-bottom:6px">Ordenar por:</div>
      <div class="seg"><button id="p-nome" class="on">Nome</button><button id="p-pts">Pontos</button></div>
    </div>
    <div id="p-corpo"></div></div>`;
  let ordem = "nome";
  const card = p => `<button class="premio ${p.disponivel ? "" : "indisp"}" onclick="pedirTroca('${esc(p.id)}','${esc(posto)}')">
      <div class="foto" ${p.foto_url ? `style="background-image:url('${esc(p.foto_url)}')"` : ""}>${p.foto_url ? "" : "🎁"}</div>
      <div class="info"><div class="n">${esc(p.nome)}</div><div class="p">${p.disponivel ? num(p.pontos) + " pontos" : "Não disponível"}</div></div></button>`;
  const desenhar = () => {
    const q = document.getElementById("p-busca").value.trim().toLowerCase();
    let ps = todos.filter(p => (!S.cat || p.categoria === S.cat) && (!q || p.nome.toLowerCase().includes(q)));
    ps.sort((a, b) => ordem === "pts" ? a.pontos - b.pontos : a.nome.localeCompare(b.nome));
    const dest = ps.filter(p => p.destaque);
    document.getElementById("p-corpo").innerHTML = !ps.length
      ? `<div class="secao"><div class="card vazio" style="padding:16px">${todos.length ? "Nada nesta busca." : "Este posto ainda não tem prêmios para troca."}</div></div>`
      : `${dest.length ? `<div class="secao"><div class="secao-tit"><h2>Destaques</h2></div><div class="destaques">${dest.map(card).join("")}</div></div>` : ""}
         <div class="secao"><div class="secao-tit"><h2>Produtos</h2></div><div class="premios">${ps.map(card).join("")}</div></div>`;
  };
  S.premiosAbertos = todos;
  document.getElementById("p-busca").addEventListener("input", desenhar);
  document.getElementById("p-nome").onclick = () => { ordem = "nome"; document.getElementById("p-nome").classList.add("on"); document.getElementById("p-pts").classList.remove("on"); desenhar(); };
  document.getElementById("p-pts").onclick = () => { ordem = "pts"; document.getElementById("p-pts").classList.add("on"); document.getElementById("p-nome").classList.remove("on"); desenhar(); };
  desenhar();
}

function pedirTroca(id, posto) {
  const p = (S.premiosAbertos || []).find(x => x.id === id);
  if (!p) return;
  const saldo = S.pontos ? S.pontos.saldo : 0;
  if (!p.disponivel) { toast("Prêmio esgotado neste momento."); return; }
  if (saldo < p.pontos) { toast(`Faltam ${num(p.pontos - saldo)} pontos para este prêmio.`); return; }
  folha(`<h2 style="font-size:1.15rem">Trocar ${num(p.pontos)} pontos?</h2>
    <div style="color:var(--mut);margin:6px 0 2px"><b style="color:var(--txt)">${esc(p.nome)}</b> no ${esc(nomePosto(posto))}.</div>
    <div style="color:var(--mut);font-size:.88rem">Você recebe um código para mostrar no caixa. Ele vale 7 dias; se não retirar, os pontos voltam.</div>
    <button class="btn" id="t-ok">Trocar</button><button class="btn sec" onclick="fecharFolha()">Agora não</button>
    <div class="msg" id="t-msg"></div>`);
  document.getElementById("t-ok").onclick = async () => {
    const b = document.getElementById("t-ok"); b.disabled = true;
    const r = await api("/cashback/api/resgatar", { premio_id: id, posto });
    if (!r.ok) { b.disabled = false; const m = document.getElementById("t-msg"); m.className = "msg erro"; m.textContent = r.erro || "Não deu para trocar."; return; }
    folha(`<div class="codigo"><span class="tag verde">Troca feita</span>
      <div style="margin-top:10px;color:var(--mut)">Mostre este código no caixa do <b style="color:var(--txt)">${esc(r.posto)}</b></div>
      <div class="num">${esc(r.codigo)}</div>
      <div><b>${esc(r.premio)}</b></div>
      <div style="color:var(--mut);font-size:.86rem;margin-top:4px">Vale até ${esc(dataBr(r.expira_em))} · saldo: ${num(r.saldo)} pontos</div>
      <button class="btn" onclick="fecharFolha();ir('#/cupons')">Ver meus cupons</button></div>`);
  };
}

// ================================================================ CUPONS
async function telaCupons() {
  const [rs, ofertas] = await Promise.all([api("/cashback/api/resgates"), api("/cashback/api/ofertas?posto=" + encodeURIComponent(S.posto || ""))]);
  const lst = Array.isArray(rs) ? rs : [];
  const vivos = lst.filter(r => r.status === "emitido"), velhos = lst.filter(r => r.status !== "emitido");
  S.ofertas = Array.isArray(ofertas) ? ofertas : S.ofertas || [];
  const ST = { usado: ["Retirado", "verde"], expirado: ["Venceu · pontos voltaram", "cinza"], cancelado: ["Cancelado", "cinza"] };
  el().innerHTML = `<div class="tela">${topoSimples("Cupons", false)}
    <div class="secao" style="padding-top:4px"><div class="secao-tit"><h2>Para retirar</h2></div>
      ${vivos.length ? vivos.map(r => `<div class="card" style="padding:16px;margin-bottom:10px;display:flex;align-items:center;gap:14px">
          <div style="flex:1"><div style="font-weight:700">${esc(r.premio_nome)}</div><div style="color:var(--mut);font-size:.84rem">${esc(r.posto)} · até ${esc(dataBr(r.expira_em))}</div></div>
          <div style="font-size:1.35rem;font-weight:800;letter-spacing:.18em;color:var(--azul)">${esc(r.codigo)}</div></div>`).join("")
        : `<div class="card vazio" style="padding:16px">Nenhum cupom para retirar. Troque seus pontos em <a class="link" href="#/resgatar">Resgatar</a>.</div>`}
    </div>
    ${S.ofertas.length ? `<div class="secao"><div class="secao-tit"><h2>Ofertas</h2></div><div class="card lista">${S.ofertas.map(o => `
      <button class="linha" onclick="ir('#/oferta/${esc(o.id)}')"><span class="ic">${ICO.megafone(20)}</span>
        <div class="meio"><div class="t">${esc(o.titulo)}</div><div class="s">${esc(o.texto || "")}</div></div><span class="seta">${ICO.seta(18)}</span></button>`).join("")}</div></div>` : ""}
    ${velhos.length ? `<div class="secao"><div class="secao-tit"><h2>Anteriores</h2></div><div class="card lista">${velhos.map(r => {
      const st = ST[r.status] || [r.status, "cinza"];
      return `<div class="linha"><div class="meio"><div class="t">${esc(r.premio_nome)}</div><div class="s">${esc(r.posto)} · ${esc(dataBr(r.usado_em || r.criado_em))}</div></div><span class="tag ${st[1]}">${esc(st[0])}</span></div>`;
    }).join("")}</div></div>` : ""}
  </div>`;
}

function telaOferta(id) {
  const o = (S.ofertas || []).find(x => x.id === id);
  if (!o) { ir("#/inicio"); return; }
  api("/cashback/api/notificacao/aberta", { campanha_id: id });
  const itens = Array.isArray(o.itens) ? o.itens : [];
  el().innerHTML = `<div class="tela">${topoSimples("Oferta", true)}
    <div class="secao" style="padding-top:2px">
      <div class="banner ${o.imagem_url ? "" : "sem-foto"}" style="flex:none;width:100%">${o.imagem_url ? `<img src="${esc(o.imagem_url)}" alt="">` : ""}
        ${o.imagem_url ? "" : `<div class="legenda"><b>${esc(o.titulo)}</b></div>`}</div>
      <h2 style="margin-top:14px;font-size:1.25rem;letter-spacing:-.01em">${esc(o.titulo)}</h2>
      <div style="color:var(--mut);margin-top:6px;white-space:pre-wrap">${esc(o.texto || "")}</div>
      ${itens.length ? `<div class="card lista" style="margin-top:14px">${itens.map(i => `<div class="linha"><span class="ic">${ICO.bomba(20)}</span>
        <div class="meio"><div class="t">${esc(i.combustivel)}</div><div class="s" style="text-decoration:line-through">de ${brl(i.de)}/L</div></div>
        <div class="dir" style="color:var(--verm);font-size:1.15rem">${brl(i.por)}<span class="s">/L</span></div></div>`).join("")}</div>` : ""}
      ${o.fim ? `<div class="vazio">Válida até ${esc(dataBr(o.fim))} às ${esc(horaBr(o.fim))}.</div>` : ""}
      ${o.link ? `<a class="btn" style="display:block;text-align:center;text-decoration:none" href="${esc(o.link)}" target="_blank" rel="noopener">Saiba mais</a>` : ""}
    </div></div>`;
}

// ================================================================ PERFIL
async function telaPerfil() {
  const me = await carregarMe();
  if (!me) { sessaoCaiu(); return; }
  const hora = new Date().getHours();
  const item = (ic, txt, acao, extra) => `<button class="linha" onclick="${acao}"><span class="ic">${ICO[ic](20)}</span><div class="meio"><div class="t">${txt}</div></div>${extra || `<span class="seta">${ICO.seta(18)}</span>`}</button>`;
  el().innerHTML = `<div class="tela">
    <div class="perfil-topo"><div class="avatar">${esc(iniciais(me.nome))}</div>
      <div><div style="color:var(--mut)">${hora < 12 ? "Bom dia" : hora < 18 ? "Boa tarde" : "Boa noite"},</div>
        <div style="font-size:1.35rem;font-weight:800;letter-spacing:-.01em">${esc(primeiroNome(me.nome))}</div>
        <div style="color:var(--mut);font-size:.84rem">Código: ${esc(me.codigo || "")}</div></div></div>
    <div class="grupo-tit">Minha conta</div>
    <div class="secao" style="padding-top:0"><div class="card lista">
      ${item("dados", "Detalhes da conta", "ir('#/dados')")}
      ${item("loja", "Postos", "ir('#/postos')")}
      ${item("estrela", "Posto favorito", "ir('#/favorito')")}
    </div></div>
    <div class="grupo-tit">Histórico</div>
    <div class="secao" style="padding-top:0"><div class="card lista">
      ${item("historico", "Histórico de compras", "ir('#/historico')")}
      ${item("relogio", "Extrato de pontos", "ir('#/extrato')")}
    </div></div>
    <div class="grupo-tit">Benefícios</div>
    <div class="secao" style="padding-top:0"><div class="card lista">
      ${item("aperto", "Clube de vantagens", "ir('#/parceiros')")}
      ${item("presente", "Trocar pontos", "ir('#/resgatar')")}
    </div></div>
    <div class="grupo-tit">Preferências</div>
    <div class="secao" style="padding-top:0"><div class="card lista">
      ${item("megafone", "Receber ofertas e promoções", "alternarMkt()", `<span class="chave ${me.aceita_marketing ? "on" : ""}" id="mkt-chave"></span>`)}
    </div></div>
    <div class="grupo-tit">Segurança</div>
    <div class="secao" style="padding-top:0"><div class="card lista">
      ${item("sair", "Sair", "sair()")}
      ${item("escudo", "Sair de todos os aparelhos", "sairTodos()")}
      ${item("lixo", "Excluir minha conta", "excluirConta()")}
    </div></div>
    <div class="vazio" style="text-align:center">Postos SN · versão ${VERSAO_APP}</div>
  </div>`;
}

async function alternarMkt() {
  const novo = !(S.me && S.me.aceita_marketing);
  const r = await api("/cashback/api/perfil", { aceita_marketing: novo });
  if (!r.ok) { toast(r.erro || "Não consegui salvar."); return; }
  if (S.me) S.me.aceita_marketing = novo;
  document.getElementById("mkt-chave").classList.toggle("on", novo);
  toast(novo ? "Pronto: você vai receber as ofertas." : "Você não vai mais receber ofertas. Os avisos da sua conta continuam.");
}

function sair() {
  folha(`<h2 style="font-size:1.15rem">Sair da conta?</h2>
    <button class="btn" onclick="LS.del('psn_token');S.me=S.pontos=null;fecharFolha();ir('#/entrar')">Sair</button>
    <button class="btn sec" onclick="fecharFolha()">Cancelar</button>`);
}
function sairTodos() {
  folha(`<h2 style="font-size:1.15rem">Sair de todos os aparelhos?</h2>
    <div style="color:var(--mut);margin-top:6px">Use se perdeu o celular ou acha que alguém sabe a sua senha. Todos os aparelhos vão precisar entrar de novo.</div>
    <button class="btn verm" id="st-ok">Sair de todos</button><button class="btn sec" onclick="fecharFolha()">Cancelar</button>`);
  document.getElementById("st-ok").onclick = async () => {
    const r = await api("/cashback/api/conta/sair-todos", {});
    if (r.ok) { LS.del("psn_token"); S.me = null; fecharFolha(); ir("#/entrar"); } else toast(r.erro || "Falhou.");
  };
}
function excluirConta() {
  folha(`<h2 style="font-size:1.15rem">Excluir minha conta</h2>
    <div style="color:var(--mut);margin-top:6px">Apaga a sua conta do app e a foto do rosto e desliga o cashback. Os pontos são perdidos. O que é registro de venda e de pagamento fica com o posto. Não dá para desfazer.</div>
    <div class="campo"><label>Sua senha</label><input type="password" id="ex-senha" autocomplete="current-password"></div>
    <button class="btn verm" id="ex-ok">Excluir definitivamente</button><button class="btn sec" onclick="fecharFolha()">Cancelar</button>
    <div class="msg" id="ex-msg"></div>`);
  document.getElementById("ex-ok").onclick = async () => {
    const r = await api("/cashback/api/conta/excluir", { senha: document.getElementById("ex-senha").value });
    if (r.ok) { LS.del("psn_token"); S.me = null; fecharFolha(); toast("Sua conta foi excluída."); ir("#/entrar"); return; }
    const m = document.getElementById("ex-msg"); m.className = "msg erro"; m.textContent = r.erro || "Não consegui excluir.";
  };
}

async function telaDados() {
  const me = await carregarMe(true);
  const linha = (rot, val) => `<div class="linha"><div class="meio"><div class="s">${rot}</div><div class="t">${esc(val || "—")}</div></div></div>`;
  el().innerHTML = `<div class="tela">${topoSimples("Detalhes da conta", true)}
    <div class="secao" style="padding-top:4px"><div class="card lista">
      ${linha("Nome", me.nome)}${linha("CPF", me.cpf_mascarado)}${linha("Celular", me.telefone)}${linha("E-mail", me.email)}${linha("Chave Pix do cashback", me.chave_pix)}
    </div><div class="vazio">Para mudar estes dados, fale com o caixa do posto. A chave Pix é onde o seu cashback cai.</div></div></div>`;
}

async function telaFavorito() {
  const [me, postos] = await Promise.all([carregarMe(), carregarPostos()]);
  el().innerHTML = `<div class="tela">${topoSimples("Posto favorito", true)}
    <div class="secao" style="padding-top:4px"><div class="vazio" style="padding-top:0">As promoções do seu posto favorito chegam primeiro para você.</div>
    <div class="card lista">${postos.map(p => `<button class="linha" onclick="salvarFavorito('${esc(p.id)}')">
      <span class="ic">${ICO.estrela(20)}</span><div class="meio"><div class="t">${esc(p.nome)}</div><div class="s">${esc(p.cidade || "")}</div></div>
      ${me && me.posto_favorito === p.id ? `<span style="color:var(--verde)">${ICO.check(22)}</span>` : ""}</button>`).join("")}</div></div></div>`;
}
async function salvarFavorito(id) {
  const r = await api("/cashback/api/perfil", { posto_favorito: id });
  if (!r.ok) { toast(r.erro || "Não consegui salvar."); return; }
  if (S.me) S.me.posto_favorito = id;
  S.posto = id; LS.set("psn_posto", id);
  toast("Posto favorito salvo.");
  telaFavorito();
}

async function telaHistorico() {
  const h = await api("/cashback/api/historico");
  const lst = Array.isArray(h) ? h : [];
  el().innerHTML = `<div class="tela">${topoSimples("Histórico de compras", true)}
    <div class="secao" style="padding-top:4px">${lst.length ? `<div class="card lista">${lst.map(linhaCompra).join("")}</div>`
      : `<div class="card vazio" style="padding:16px">Nenhuma compra identificada ainda. Informe o seu CPF no caixa ou acione pelo botão da bomba.</div>`}</div></div>`;
}

async function telaExtrato() {
  const r = await api("/cashback/api/pontos");
  const T = { ganho: "Ganhou", estorno: "Pontos devolvidos", resgate: "Trocou", vencido: "Venceram", cancelado: "Venda cancelada", ajuste: "Ajuste" };
  el().innerHTML = `<div class="tela">${topoSimples("Extrato de pontos", true)}
    <div class="secao" style="padding-top:4px">
      <div class="card" style="padding:16px;display:flex;justify-content:space-between;align-items:center">
        <div><div class="s" style="color:var(--mut);font-size:.8rem">Saldo</div><div style="font-size:1.6rem;font-weight:800;color:var(--ouro)">${num(r.saldo || 0)} pontos</div></div>
        <button class="btn-borda" onclick="ir('#/resgatar')">Trocar</button></div>
      ${r.a_vencer_30d > 0 ? `<div class="vazio">⏳ ${num(r.a_vencer_30d)} ponto(s) vencem nos próximos 30 dias.</div>` : ""}
      <div class="vazio" style="padding-bottom:6px">1 ponto a cada R$ 1 nas compras identificadas. Cada ponto vale 6 meses.</div>
      ${(r.extrato || []).length ? `<div class="card lista">${r.extrato.map(m => `<div class="linha">
        <div class="meio"><div class="t">${esc(T[m.tipo] || m.tipo)}</div><div class="s">${esc(m.obs || "")}${m.posto ? " · " + esc(m.posto) : ""} · ${esc(dataBr(m.quando))}</div></div>
        <div class="dir" style="color:${m.pontos >= 0 ? "var(--verde)" : "var(--verm)"}">${m.pontos > 0 ? "+" : ""}${num(m.pontos)}</div></div>`).join("")}</div>`
        : '<div class="card vazio" style="padding:16px">Nenhum ponto ainda.</div>'}
    </div></div>`;
}

async function telaParceiros() {
  const lst = await api("/cashback/api/parceiros?posto=" + encodeURIComponent(S.posto || ""));
  const ps = Array.isArray(lst) ? lst : [];
  el().innerHTML = `<div class="tela">${topoSimples("Clube de vantagens", true)}
    <div class="secao" style="padding-top:4px">${ps.length ? ps.map(p => `<div class="card" style="padding:14px;margin-bottom:10px">
      <div style="display:flex;gap:12px;align-items:center">
        ${p.logo_url ? `<img src="${esc(p.logo_url)}" alt="" style="width:52px;height:52px;object-fit:contain;border-radius:12px;border:1px solid var(--borda)">` : `<span class="ic" style="width:52px;height:52px;border-radius:12px;background:var(--azul-claro);color:var(--azul);display:flex;align-items:center;justify-content:center">${ICO.aperto(22)}</span>`}
        <div><div style="font-weight:700">${esc(p.nome)}</div><div style="color:var(--verm);font-weight:700;font-size:.9rem">${esc(p.beneficio)}</div></div></div>
      ${p.descricao ? `<div style="color:var(--mut);font-size:.86rem;margin-top:8px;white-space:pre-wrap">${esc(p.descricao)}</div>` : ""}
      <div style="color:var(--mut);font-size:.8rem;margin-top:6px">${esc([p.endereco, p.cidade, p.telefone].filter(Boolean).join(" · "))}</div></div>`).join("")
      : '<div class="card vazio" style="padding:16px">Em breve, parceiros com descontos para quem é cliente dos Postos SN.</div>'}</div></div>`;
}

async function telaNotificacoes() {
  const lst = await api("/cashback/api/notificacoes");
  const ns = Array.isArray(lst) ? lst : [];
  el().innerHTML = `<div class="tela">${topoSimples("Notificações", true)}
    <div class="secao" style="padding-top:4px">${ns.length ? `<div class="card lista">${ns.map(n => `
      <button class="linha" onclick="${n.campanha_id ? `ir('#/oferta/${esc(n.campanha_id)}')` : ""}"><span class="ic">${ICO[n.tipo === "aviso" ? "sino" : "megafone"](20)}</span>
        <div class="meio"><div class="t">${esc(n.titulo)}</div><div class="s">${esc(n.texto || "")}</div><div class="s">${esc(dataBr(n.enviado_em))} ${esc(horaBr(n.enviado_em))}</div></div></button>`).join("")}</div>`
      : '<div class="card vazio" style="padding:16px">Nenhuma notificação ainda. As promoções e os avisos da sua conta aparecem aqui e na tela do celular.</div>'}</div></div>`;
}

const TELAS = {
  entrar: telaEntrar, cadastro: telaCadastro, confirmar: telaConfirmar, esqueci: telaEsqueci, equipe: a => telaEquipe(a),
  inicio: telaInicio, resgatar: () => telaResgatar(""), postos: () => telaResgatar("postos"), premios: telaPremios,
  cupons: telaCupons, oferta: telaOferta, perfil: telaPerfil, dados: telaDados, favorito: telaFavorito,
  historico: telaHistorico, extrato: telaExtrato, parceiros: telaParceiros, notificacoes: telaNotificacoes,
  bomba: a => telaBomba(a), acionar: a => telaAcionar(a),
};

window.addEventListener("hashchange", rota);
window.addEventListener("load", rota);
