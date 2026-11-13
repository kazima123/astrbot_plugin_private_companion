"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "../astrbot_host/native_extensions/game/web/native.js"), "utf8");
const gomokuSource = fs.readFileSync(path.join(__dirname, "../astrbot_host/native_extensions/game/web/native-gomoku.js"), "utf8");
const inputSource = fs.readFileSync(path.join(__dirname, "../astrbot_host/native_extensions/game/web/native-gomoku-input.js"), "utf8");

class Element {
  constructor(tag = "div") {
    this.tag = tag; this.children = []; this.dataset = {}; this.attributes = {}; this.listeners = {};
    this.className = ""; this.text = ""; this.style = {setProperty() {}};
    this.classList = {
      add: (...values) => { this.className = [...new Set([...this.className.split(/\s+/).filter(Boolean), ...values])].join(" "); },
      toggle: (name, enabled) => { this.className = this.className.split(/\s+/).filter(value => value !== name).join(" ");
        if (enabled) this.classList.add(name); },
    };
  }
  set textContent(value) { this.text = String(value); this.children = []; }
  get textContent() { return this.text + this.children.map(child => child.textContent).join(" "); }
  append(...children) { this.children.push(...children); children.forEach(child => { child.parent = this; }); }
  appendChild(child) { this.append(child); child.parent = this; return child; }
  replaceChildren(...children) { this.text = ""; this.children = children; }
  setAttribute(name, value) { this.attributes[name] = value; }
  addEventListener(name, callback) { this.listeners[name] = callback; }
  querySelector(selector) {
    return descendants(this).slice(1).find(child => selector.startsWith(".")
      ? child.className.split(/\s+/).includes(selector.slice(1)) : child.tag === selector) || null;
  }
  getBoundingClientRect() { return {left: 100, top: 100, width: 600, height: 600}; }
  remove() { if (this.parent) this.parent.children = this.parent.children.filter(child => child !== this); }
  focus() { if (this.onFocus) this.onFocus(this); }
  setPointerCapture(id) { this.capturedPointer = id; }
  releasePointerCapture(id) { if (this.capturedPointer === id) this.capturedPointer = null; }
}

function descendants(element) { return [element, ...element.children.flatMap(descendants)]; }

async function render(game, {confirm = false, fetchAction, fetchState} = {}) {
  const elements = new Map();
  const $ = id => { if (!elements.has(id)) elements.set(id, new Element()); return elements.get(id); };
  $("connectionBadge").append(new Element("span"));
  const requests = [];
  const events = {};
  let poll;
  const document = {getElementById: $, activeElement: null, createElement: tag => {
    const element = new Element(tag); element.onFocus = value => { document.activeElement = value; }; return element;
  }};
  document.createElementNS = (_, tag) => document.createElement(tag);
  const context = vm.createContext({document,
    window: {location: {pathname: "/native/fixture-ticket"}, matchMedia: () => ({matches: confirm}),
      setInterval(callback) { poll = callback; return 1; }, clearInterval() {},
      addEventListener(name, callback) { events[name] = callback; },
      removeEventListener(name, callback) { if (events[name] === callback) delete events[name]; }},
    fetch: async (url, options) => {
      requests.push({url, options});
      const value = options.method === "POST" ? await (fetchAction?.(JSON.parse(options.body)) ?? {game})
        : await (fetchState?.() ?? {game});
      return {ok: true, json: async () => value};
    },
  });
  vm.runInContext(inputSource, context);
  vm.runInContext(gomokuSource, context);
  vm.runInContext(source, context);
  await new Promise(resolve => setImmediate(resolve));
  assert.equal($("notice").textContent, "");
  assert.equal(requests.length, 1);
  assert.equal(requests[0].options.method, undefined);
  return {$, requests, poll, events, document};
}

function boardGame(kind, human, status = "active", outcome = null) {
  const board = Array.from({length: kind === "gomoku" ? 15 : 3}, () => Array(kind === "gomoku" ? 15 : 3).fill(0));
  board[0][0] = 1; board[1][2] = 2;
  return {game_type: kind, status, outcome, moves: [[0, 0, 1], [1, 2, 2]], state: {
    board, human_color: human, bot_color: human === 1 ? 2 : 1,
    human_mark: kind === "tictactoe" ? 1 : undefined, move_count: 2, turn: human,
    pieces: [{piece_id: "piece-1", row: 0, column: 0, color: 1, x: .5, y: .5},
             {piece_id: "piece-2", row: 1, column: 2, color: 2, x: .5, y: .5}],
  }};
}

test("五子棋按实际黑白颜色绘制，用户执白也不反色，落子历史归属与行列完整", async () => {
  for (const human of [1, 2]) {
    const {$} = await render(boardGame("gomoku", human));
    const pieces = descendants($("gameBoard")).filter(node => node.dataset.pieceId);
    assert.match(pieces[0].children[0].className, /stone-black/);
    assert.match(pieces[1].children[0].className, /stone-white/);
    const scores = $("gameSummary").children[0].children;
    assert.equal(scores[0].children[1].textContent, human === 1 ? "黑棋" : "白棋");
    assert.equal(scores[1].children[1].textContent, human === 1 ? "白棋" : "黑棋");
    const history = $("gameHistory").children[0].children.filter(child => child.tag === "span");
    assert.equal(history[0].textContent, human === 2 ? "你" : "陪伴者");
    assert.equal(history[1].textContent, "第 2 行，第 3 列");
  }
});

const flush = () => new Promise(resolve => setImmediate(resolve));
const click = (element) => element.listeners.click({stopPropagation() {}, clientX: 1, clientY: 999});
const point = ($, row, column) => descendants($("gameBoard")).find(element =>
  element.dataset.focusKey === `point-${row}-${column}`);
const byText = (host, text) => descendants(host).find(element => element.tag === "button" && element.textContent.trim() === text);

test("正常对弈点击交叉点自动应手，旧偏移棋子归位，不会拿起对方棋子", async () => {
  const game = boardGame("gomoku", 2);
  game.state.pieces[0].x = .02; game.state.pieces[0].y = .98;
  const {$, requests} = await render(game);
  const stone = descendants($("gameBoard")).find(element => element.dataset.pieceId === "piece-1");
  assert.equal(stone.style.left, `${(.5 / 15) * 100}%`);
  assert.equal(stone.disabled, true);
  assert.equal(stone.listeners.pointerdown, undefined);
  assert.equal($("pieceTray").hidden, true);
  click(point($, 7, 7));
  await flush();
  const actions = requests.filter(request => request.options.method === "POST");
  assert.equal(actions.length, 1);
  assert.deepEqual(JSON.parse(actions[0].options.body), {action: "move", row: 7, column: 7, x: .5, y: .5});
});

test("手机先选点再确认，取消与轮询不落子，未变棋盘保持原 DOM", async () => {
  const {$, requests, poll} = await render(boardGame("gomoku", 1), {confirm: true});
  click(point($, 7, 7));
  const board = $("gameBoard").children[0];
  await poll();
  assert.equal($("gameBoard").children[0], board);
  assert.match($("actionControls").textContent, /落点 H8/);
  assert.equal(requests.filter(request => request.options.method === "POST").length, 0);
  click(byText($("actionControls"), "取消"));
  assert.equal(byText($("actionControls"), "确认落子").disabled, true);
  click(point($, 6, 8));
  const confirmButton = byText($("actionControls"), "确认落子");
  click(confirmButton); click(confirmButton);
  await flush();
  const actions = requests.filter(request => request.options.method === "POST");
  assert.equal(actions.length, 1);
  assert.equal(JSON.parse(actions[0].options.body).row, 6);
  assert.equal(JSON.parse(actions[0].options.body).column, 8);
});

const pointer = (x, y, extras = {}) => ({pointerId: 1, clientX: x, clientY: y, button: 0,
  preventDefault() {}, stopPropagation() {}, ...extras});
const part = ($, className) => descendants($("gameBoard")).find(element => element.className.split(/\s+/).includes(className));

test("触摸滑动选点有放大预览，松手只预选，确认后执行正常落子", async () => {
  const {$, requests, events} = await render(boardGame("gomoku", 1), {confirm: true});
  const start = point($, 7, 7);
  start.listeners.pointerdown(pointer(400, 400, {pointerType: "touch"}));
  assert.equal(part($, "gomoku-lens").hidden, false);
  events.pointermove(pointer(480, 360, {pointerType: "touch"}));
  assert.match(part($, "gomoku-interaction-readout").textContent, /J7/);
  assert.equal(part($, "gomoku-drag-piece").style.top, "228px");
  events.pointerup(pointer(480, 360, {pointerType: "touch"}));
  click(start);
  assert.equal(requests.filter(request => request.options.method === "POST").length, 0);
  assert.match($("actionControls").textContent, /落点 J7/);
  click(byText($("actionControls"), "确认落子"));
  await flush();
  assert.deepEqual(JSON.parse(requests.find(request => request.options.method === "POST").options.body),
    {action: "move", row: 6, column: 9, x: .5, y: .5});
});

test("请求未完成时暂停轮询，旧读取不会覆盖已提交的新棋盘", async () => {
  const oldGame = boardGame("gomoku", 1);
  const newGame = JSON.parse(JSON.stringify(oldGame));
  newGame.moves.push([7, 7, 1], [7, 8, 2]);
  newGame.state.board[7][7] = 1; newGame.state.board[7][8] = 2; newGame.state.move_count = 4;
  let finishPoll;
  let finishAction;
  let reads = 0;
  const {$, requests, poll} = await render(oldGame, {
    fetchState: () => ++reads === 1 ? {game: oldGame} : new Promise(resolve => { finishPoll = resolve; }),
    fetchAction: () => new Promise(resolve => { finishAction = resolve; }),
  });
  const slowPoll = poll();
  click(point($, 7, 7));
  await poll();
  assert.equal(reads, 2);
  finishAction({game: newGame}); await flush();
  finishPoll({game: oldGame}); await slowPoll;
  assert.match($("gameSummary").textContent, /已落子 4 手/);
  assert.equal(requests.filter(request => request.options.method === "POST").length, 1);
});

test("响应丢失后只核对状态，保留已落棋子，不自动重放操作", async () => {
  const game = boardGame("gomoku", 1);
  const committed = JSON.parse(JSON.stringify(game));
  committed.state.move_count = 4;
  let reads = 0;
  const {$, requests, poll} = await render(game, {
    fetchState: () => ({game: ++reads === 1 ? game : committed}),
    fetchAction: () => { throw new Error("connection lost"); },
  });
  click(point($, 7, 7)); await flush(); await poll();
  assert.match($("gameSummary").textContent, /已落子 4 手/);
  assert.equal(requests.filter(request => request.options.method === "POST").length, 1);
});

test("结束棋局标明最后一手和真实五子连线，可选择显示每一手编号", async () => {
  const game = boardGame("gomoku", 2, "finished", "bot");
  game.state.board = Array.from({length: 15}, () => Array(15).fill(0));
  game.moves = Array.from({length: 5}, (_, index) => [7 + index, 7 - index, 1]);
  game.moves.forEach(([row, column, color]) => { game.state.board[row][column] = color; });
  game.state.winner = 1; game.state.last_move = [11, 3];
  const {$} = await render(game);
  const stones = descendants($("gameBoard")).filter(element => element.dataset.pieceId);
  assert.equal(stones.filter(element => element.className.includes("winning-piece")).length, 5);
  assert.equal(stones.filter(element => element.className.includes("last-move")).length, 1);
  const line = descendants($("gameBoard")).find(element => element.attributes.class === "gomoku-winning-line");
  assert.equal(line.children[0].attributes.x1, "225");
  assert.equal(line.children[0].attributes.x2, "105");
  assert.equal(line.children[0].attributes.y2, "345");
  assert.equal(stones[4].attributes["aria-label"].includes("最后一手"), true);
  click(byText($("boardToolbar"), "显示手数"));
  assert.equal(descendants($("gameBoard")).find(element => element.dataset.pieceId === "piece-5").children[0].textContent, "5");
});

test("已结束的棋局展示真实输赢，不再显示轮到谁或思考中", async () => {
  for (const [outcome, label] of [["player", "你赢了"], ["bot", "陪伴者赢了"], ["draw", "本局平局"]]) {
    const {$} = await render(boardGame("gomoku", 2, "finished", outcome));
    assert.equal($("roundState").textContent, "本局结束");
    assert.match($("gameSummary").textContent, new RegExp(label));
    assert.doesNotMatch($("gameSummary").textContent, /轮到|思考中/);
    assert.equal($("actionControls").children.length, 0);
    assert.equal($("pieceTray").hidden, true);
  }
});

test("井字棋仍显示圈叉，并按棋子标记区分双方", async () => {
  const {$} = await render(boardGame("tictactoe", 1));
  const pieces = descendants($("gameBoard")).filter(node => node.dataset.pieceId);
  assert.equal(pieces[0].textContent, "×");
  assert.equal(pieces[1].textContent, "○");
  assert.equal($("gameHistory").children[0].children[0].textContent, "陪伴者");
  assert.equal($("gameHistory").children[1].children[0].textContent, "你");
});

test("正式对弈不提供自由摆子、棋盒或单独替陪伴者落子入口", async () => {
  const {$} = await render(boardGame("gomoku", 1));
  assert.equal(byText($("gameSummary"), "自由摆子"), undefined);
  assert.equal(byText($("actionControls"), "让陪伴者接着下"), undefined);
  assert.equal($("pieceTray").hidden, true);
});

test("井字棋不能拖动已有棋子，终局清楚显示记忆保存状态", async () => {
  for (const [state, text] of [["pending", "待保存"], ["saved", "已保存"], ["failed", "保存失败"]]) {
    const game = boardGame("tictactoe", 1, "closed");
    game.memory = {state};
    const {$} = await render(game);
    assert.match($("gameSummary").textContent, new RegExp(text));
    assert.match($("gameSummary").textContent, /未分胜负/);
    assert.equal($("pieceTray").hidden, true);
    for (const piece of descendants($("gameBoard")).filter(node => node.dataset.pieceId)) {
      assert.equal(piece.disabled, true);
      assert.equal(piece.listeners.pointerdown, undefined);
    }
  }
});
