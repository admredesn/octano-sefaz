// ============================================================
// App Postos SN — base: servidor, sessão, ícones e utilidades (05/10/2026)
// ------------------------------------------------------------
// O mesmo código roda de dois jeitos:
//  * versão web em https://<servidor>/app/ (o Ronan testa no iPhone antes das lojas):
//    o servidor é o próprio endereço (API = "");
//  * dentro do app das lojas (Capacitor): a página vem do celular, então o servidor
//    é o endereço completo.
// ============================================================
const SERVIDOR = "https://octano-sefaz-production-66d4.up.railway.app";
const API = (location.protocol.indexOf("http") === 0 && location.pathname.indexOf("/app") === 0) ? "" : SERVIDOR;
const VERSAO_APP = "0.1.0";
// ACESSO DA EQUIPE: as telas existem (equipe.js), mas as rotas do servidor ainda não. Enquanto
// estiver false, o link "Acesso da equipe" some da entrada e a tela diz "em breve". Ligar junto
// com a publicação das rotas /app/equipe/api/*. (var, não const: a bancada de layout liga.)
var EQUIPE_NO_AR = false;

// armazenamento local tolerante (aba anônima / bloqueio de cookies não pode derrubar o app)
const LS = {
  get(k) { try { return localStorage.getItem(k); } catch (e) { return null; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch (e) { /* sem armazenamento */ } },
  del(k) { try { localStorage.removeItem(k); } catch (e) { /* sem armazenamento */ } },
};
function tok() { return LS.get("psn_token") || ""; }

// estado da sessão (cache para as telas abrirem na hora)
const S = { me: null, pontos: null, postos: null, ofertas: null, posto: LS.get("psn_posto") || "", bico: "" };

async function api(caminho, corpo, metodo) {
  let r;
  try {
    r = await fetch(API + caminho, {
      method: metodo || (corpo ? "POST" : "GET"),
      headers: { "Content-Type": "application/json", Authorization: "Bearer " + tok() },
      body: corpo ? JSON.stringify(corpo) : undefined,
    });
  } catch (e) {
    return { http: 0, erro: "Sem conexão com a internet. Tente de novo." };
  }
  let j;
  try { j = await r.json(); } catch (e) { j = { erro: "Resposta inválida do servidor (" + r.status + ")." }; }
  if (Array.isArray(j)) { j.http = r.status; return j; }
  if (r.status === 401 && tok() && /sess/i.test(j.erro || "")) sessaoCaiu();
  return Object.assign({ http: r.status }, j);
}

function sessaoCaiu() {
  LS.del("psn_token");
  S.me = S.pontos = null;
  if (location.hash.indexOf("#/entrar") !== 0) location.hash = "#/entrar";
}

function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function brl(v) { return "R$ " + Number(v || 0).toLocaleString("pt-BR", { minimumFractionDigits: 2, maximumFractionDigits: 2 }); }
function num(v) { return Number(v || 0).toLocaleString("pt-BR"); }
function dataBr(iso) { return iso ? new Date(iso).toLocaleDateString("pt-BR") : ""; }
function horaBr(iso) { return iso ? new Date(iso).toLocaleTimeString("pt-BR", { hour: "2-digit", minute: "2-digit" }) : ""; }
function soDigitos(s) { return String(s || "").replace(/\D/g, ""); }
function primeiroNome(n) { const p = String(n || "").trim().split(/\s+/)[0] || ""; return p.charAt(0) + p.slice(1).toLowerCase(); }
function iniciais(n) {
  const p = String(n || "").trim().split(/\s+/).filter(Boolean);
  return ((p[0] || "")[0] || "") + ((p.length > 1 ? p[p.length - 1] : "")[0] || "").toLowerCase();
}

function toast(txt, ms) {
  const t = document.createElement("div");
  t.className = "toast";
  t.textContent = txt;
  document.body.appendChild(t);
  setTimeout(() => t.remove(), ms || 2800);
}

// folha de baixo: confirmação dentro da página (confirm() do navegador some em app embutido)
function folha(html) {
  fecharFolha();
  const f = document.createElement("div");
  f.className = "folha-fundo";
  f.id = "folha";
  f.innerHTML = `<div class="folha">${html}</div>`;
  f.addEventListener("click", e => { if (e.target === f) fecharFolha(); });
  document.body.appendChild(f);
}
function fecharFolha() { const f = document.getElementById("folha"); if (f) f.remove(); }

// máscaras
function mascaraCpf(el) {
  el.addEventListener("input", () => {
    const v = soDigitos(el.value).slice(0, 11);
    el.value = v.replace(/(\d{3})(\d)/, "$1.$2").replace(/(\d{3})(\d)/, "$1.$2").replace(/(\d{3})(\d{1,2})$/, "$1-$2");
  });
}
function mascaraFone(el) {
  el.addEventListener("input", () => {
    const v = soDigitos(el.value).slice(0, 11);
    el.value = v.length > 10 ? v.replace(/(\d{2})(\d{5})(\d{0,4})/, "($1) $2-$3")
      : v.replace(/(\d{2})(\d{4})(\d{0,4})/, "($1) $2-$3").replace(/-$/, "");
  });
}

// distância em km entre dois pontos (lista de postos por proximidade)
function distKm(a, b) {
  const R = 6371, rad = x => x * Math.PI / 180;
  const dLat = rad(b.lat - a.lat), dLon = rad(b.lon - a.lon);
  const h = Math.sin(dLat / 2) ** 2 + Math.cos(rad(a.lat)) * Math.cos(rad(b.lat)) * Math.sin(dLon / 2) ** 2;
  return 2 * R * Math.asin(Math.sqrt(h));
}

// ícones (traço simples, 24x24)
const ICO = (() => {
  const s = (p, t) => `<svg width="${t || 24}" height="${t || 24}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${p}</svg>`;
  const P = {
    inicio: '<path d="M3 10.5 12 3l9 7.5"/><path d="M5 9.5V21h5v-6h4v6h5V9.5"/>',
    presente: '<rect x="3" y="8" width="18" height="4" rx="1"/><path d="M12 8v13M5 12v9h14v-9"/><path d="M12 8c-1.5-3-5-3-5-1s3 1 5 1c2 0 5 1 5-1s-3.5-2-5 1Z"/>',
    bomba: '<path d="M4 21V5a2 2 0 0 1 2-2h7a2 2 0 0 1 2 2v16"/><path d="M3 21h13"/><path d="M7 7h5v4H7z"/><path d="M15 9h2a2 2 0 0 1 2 2v6a1.5 1.5 0 0 0 3 0V8l-3-3"/>',
    cupom: '<path d="M3 8a2 2 0 0 0 2-2h14a2 2 0 0 0 2 2v2a2 2 0 0 0 0 4v2a2 2 0 0 0-2 2H5a2 2 0 0 0-2-2v-2a2 2 0 0 0 0-4Z"/><path d="m9 15 6-6"/><circle cx="9.5" cy="9.5" r=".5"/><circle cx="14.5" cy="14.5" r=".5"/>',
    perfil: '<circle cx="12" cy="8" r="4"/><path d="M4 21a8 8 0 0 1 16 0"/>',
    sino: '<path d="M6 8a6 6 0 0 1 12 0c0 7 3 9 3 9H3s3-2 3-9"/><path d="M10.3 21a1.9 1.9 0 0 0 3.4 0"/>',
    voltar: '<path d="m15 18-6-6 6-6"/>',
    seta: '<path d="m9 18 6-6-6-6"/>',
    oleo: '<path d="M7 6h7l3 3v10a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2Z"/><path d="M9 3h3v3H9z"/><path d="M10.5 12.5c-1 1.4-1.5 2.2-1.5 3a1.5 1.5 0 0 0 3 0c0-.8-.5-1.6-1.5-3Z"/>',
    servico: '<path d="M14.7 6.3a4 4 0 0 0-5.4 5.4L3 18l3 3 6.3-6.3a4 4 0 0 0 5.4-5.4l-2.6 2.6-2.4-.6-.6-2.4Z"/>',
    loja: '<path d="M3 9 5 4h14l2 5"/><path d="M3 9a3 3 0 0 0 6 0 3 3 0 0 0 6 0 3 3 0 0 0 6 0"/><path d="M5 12v9h14v-9"/><path d="M10 21v-5h4v5"/>',
    aditivo: '<path d="M9 3h6"/><path d="M10 3v6l-5 9a2 2 0 0 0 1.7 3h10.6a2 2 0 0 0 1.7-3l-5-9V3"/><path d="M7.5 14h9"/>',
    local: '<path d="M12 21s7-6.2 7-12a7 7 0 0 0-14 0c0 5.8 7 12 7 12Z"/><circle cx="12" cy="9" r="2.5"/>',
    estrela: '<path d="m12 3 2.7 5.6 6.1.9-4.4 4.3 1 6.1L12 17l-5.4 2.9 1-6.1-4.4-4.3 6.1-.9Z"/>',
    historico: '<path d="M3 12a9 9 0 1 0 3-6.7L3 8"/><path d="M3 3v5h5"/><path d="M12 7v5l3 2"/>',
    relogio: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
    trofeu: '<path d="M8 21h8M12 17v4"/><path d="M7 4h10v5a5 5 0 0 1-10 0Z"/><path d="M17 5h3a3 3 0 0 1-3 4M7 5H4a3 3 0 0 0 3 4"/>',
    aperto: '<path d="m11 17 2 2a1.4 1.4 0 0 0 2-2"/><path d="m14 14 2.5 2.5a1.4 1.4 0 0 0 2-2l-3.9-3.9a2 2 0 0 0-2.8 0l-.9.9a1.4 1.4 0 1 1-2-2l2.8-2.8a3.6 3.6 0 0 1 4.6-.4l.6.4a3 3 0 0 0 2 .5L21 7"/><path d="m21 3 1 11h-2"/><path d="M3 3 2 14l6.5 6.5a1.4 1.4 0 0 0 2-2"/><path d="M3 4h8"/>',
    sair: '<path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><path d="m16 17 5-5-5-5"/><path d="M21 12H9"/>',
    lixo: '<path d="M3 6h18"/><path d="M8 6V4h8v2"/><path d="M6 6l1 15h10l1-15"/>',
    teclado: '<rect x="2" y="6" width="20" height="12" rx="2"/><path d="M6 10h.01M10 10h.01M14 10h.01M18 10h.01M7 14h10"/>',
    qr: '<rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><path d="M14 14h3v3M21 14v.01M17 21h4v-4M14 21v-.01"/>',
    camera: '<path d="M4 7h3l2-3h6l2 3h3a1 1 0 0 1 1 1v11a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V8a1 1 0 0 1 1-1Z"/><circle cx="12" cy="13" r="4"/>',
    x: '<path d="M18 6 6 18M6 6l12 12"/>',
    escudo: '<path d="M12 3 4 6v6c0 5 3.5 8 8 9 4.5-1 8-4 8-9V6Z"/>',
    dados: '<rect x="3" y="4" width="18" height="16" rx="2"/><circle cx="9" cy="10" r="2"/><path d="M6 16a3 3 0 0 1 6 0M14 9h4M14 13h4"/>',
    megafone: '<path d="M3 11v2a1 1 0 0 0 1 1h3l6 4V6L7 10H4a1 1 0 0 0-1 1Z"/><path d="M17 9a4 4 0 0 1 0 6"/>',
    check: '<path d="m5 12 5 5L20 7"/>',
    // equipe
    painel: '<rect x="3" y="3" width="8" height="10" rx="1.5"/><rect x="13" y="3" width="8" height="6" rx="1.5"/><rect x="13" y="12" width="8" height="9" rx="1.5"/><rect x="3" y="16" width="8" height="5" rx="1.5"/>',
    aprovar: '<circle cx="12" cy="12" r="9"/><path d="m8 12.5 3 3 5-6"/>',
    desconto: '<path d="M19 5 5 19"/><circle cx="7" cy="7" r="2.5"/><circle cx="17" cy="17" r="2.5"/>',
    prazo: '<rect x="2" y="5" width="20" height="14" rx="2"/><path d="M2 10h20M6 15h4"/>',
    alerta: '<path d="M12 3 2 20h20Z"/><path d="M12 10v4M12 17v.01"/>',
    tanque: '<ellipse cx="12" cy="6" rx="7" ry="3"/><path d="M5 6v12c0 1.7 3.1 3 7 3s7-1.3 7-3V6"/><path d="M5 12c0 1.7 3.1 3 7 3s7-1.3 7-3"/>',
  };
  const out = {};
  Object.keys(P).forEach(k => { out[k] = t => s(P[k], t); });
  return out;
})();
