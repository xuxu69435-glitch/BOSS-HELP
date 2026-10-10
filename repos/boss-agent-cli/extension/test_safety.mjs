import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const source = fs.readFileSync(new URL('./background.js', import.meta.url), 'utf8');
function runtime(tabs = []) {
	const injections = [];
	const requests = [];
	const listeners = {};
	let reloads = 0;
	let context;
	const chrome = {
		permissions: {contains: async () => true},
		alarms: {create() {}, onAlarm: {addListener() {}}},
		windows: {onRemoved: {addListener() {}}, create() { throw new Error('Must not create a browser window'); }},
		tabs: {
			create() {throw new Error('Must not create a tab');},
			update() {throw new Error('Must not navigate or activate a tab');},
			remove() {throw new Error('Must not close a tab');},
			query: async () => tabs,
			get: async id => {
				const tab = tabs.find(tab => tab.id === id);
				if (!tab) throw new Error(`No tab with id: ${id}.`);
				return tab;
			},
			onRemoved: {addListener(callback) {listeners.removed = callback;}},
			onReplaced: {addListener(callback) {listeners.replaced = callback;}},
		},
		scripting: {executeScript: async options => {
			assert.equal(options.func.name, 'pageCommand');
			assert.equal(options.world, ['inspect-search', 'collect-native', 'inspect-chat-identity', 'select-chat', 'prepare-chat', 'send-chat'].includes(options.args[0]) ? 'MAIN' : 'ISOLATED');
			assert.equal(options.target.frameIds.join(','), '0');
			injections.push(options);
			context.location = new URL(tabs.find(tab => tab.id === options.target.tabId).url);
			return [{frameId: 0, result: await options.func(...options.args)}];
		}},
		runtime: {reload() {reloads++;}, onInstalled: {addListener() {}}, onStartup: {addListener() {}}, onMessage: {addListener() {}}},
	};
	Object.defineProperty(chrome, 'debugger', {get() {throw new Error('Debugger access is forbidden');}});
	context = vm.createContext({chrome, URL, URLSearchParams, innerWidth: 1200, innerHeight: 900, getComputedStyle: () => ({display: 'block', visibility: 'visible', opacity: '1'}), performance: {timeOrigin: 1000}, document: {title: 'Fixture BOSS', body: {innerText: 'Fixture content'}, querySelector() {return null;}, querySelectorAll() {return [];}}, console: {log() {}, error() {}}, setTimeout, clearTimeout, AbortSignal, WebSocket: {OPEN: 1, CONNECTING: 0}, fetch: async (url, options) => {
		if (String(url).startsWith('http://127.0.0.1:')) return {ok: false};
		requests.push({url, options});
		return {status: 200, text: async () => '{"code":0,"zpData":{"fixture":true,"mid":9007199254740993}}'};
	}});
	vm.runInContext(source, context);
	context.initialize();
	return {context, injections, requests, chrome, listeners, reloadCount: () => reloads};
}

const missing = runtime();
await assert.rejects(missing.context.resolveTabId(undefined, 'boss'), /No existing BOSS tab/);
const multiple = runtime([{id: 1, url: 'https://www.zhipin.com/web/geek/jobs'}, {id: 2, url: 'https://www.zhipin.com/web/geek/chat'}]);
await assert.rejects(multiple.context.resolveTabId(undefined, 'boss'), /Multiple BOSS tabs/);
const selectedTabs = [{id: 1, url: 'https://www.zhipin.com/web/geek/jobs', active: false}, {id: 2, url: 'https://www.zhipin.com/job_detail/test.html', active: true}];
const active = runtime(selectedTabs);
assert.equal(await active.context.resolveTabId(undefined, 'boss'), 2);
selectedTabs[0].active = true;
selectedTabs[1].active = false;
assert.equal(await active.context.resolveTabId(undefined, 'boss'), 2);
selectedTabs[1].url = 'https://example.com/';
await assert.rejects(active.context.resolveTabId(undefined, 'boss'), /Selected BOSS tab changed/);
const staleTabs = [{id: 10, url: 'https://www.zhipin.com/beijing/', active: false}];
const stale = runtime(staleTabs);
assert.equal(await stale.context.resolveTabId(undefined, 'boss'), 10);
staleTabs.splice(0, 1, {id: 11, url: 'https://www.zhipin.com/beijing/', active: false});
assert.equal(await stale.context.resolveTabId(undefined, 'boss'), 11);
stale.listeners.removed(11);
staleTabs.splice(0, 1, {id: 12, url: 'https://www.zhipin.com/beijing/', active: false});
assert.equal(await stale.context.resolveTabId(undefined, 'boss'), 12);
const replacedTabs = [{id: 20, url: 'https://www.zhipin.com/beijing/', active: true}];
const replaced = runtime(replacedTabs);
assert.equal(await replaced.context.resolveTabId(undefined, 'boss'), 20);
replacedTabs.splice(0, 1, {id: 21, url: 'https://www.zhipin.com/beijing/', active: false}, {id: 22, url: 'https://www.zhipin.com/web/geek/jobs', active: true});
replaced.listeners.replaced(21, 20);
assert.equal(await replaced.context.resolveTabId(undefined, 'boss'), 21);
replacedTabs[0].url = 'https://example.com/';
await assert.rejects(replaced.context.resolveTabId(undefined, 'boss'), /Selected BOSS tab changed/);
const ambiguousTabs = [{id: 30, url: 'https://www.zhipin.com/beijing/'}];
const ambiguous = runtime(ambiguousTabs);
assert.equal(await ambiguous.context.resolveTabId(undefined, 'boss'), 30);
ambiguousTabs.splice(0, 1, {id: 31, url: 'https://www.zhipin.com/beijing/'}, {id: 32, url: 'https://www.zhipin.com/shanghai/'});
await assert.rejects(ambiguous.context.resolveTabId(undefined, 'boss'), /Multiple BOSS tabs/);
const denied = runtime([{id: 40, url: 'https://www.zhipin.com/beijing/'}]);
assert.equal(await denied.context.resolveTabId(undefined, 'boss'), 40);
denied.chrome.tabs.get = async () => {throw new Error('Permission denied');};
await assert.rejects(denied.context.resolveTabId(undefined, 'boss'), /Permission denied/);
const valid = runtime([{id: 1, url: 'https://www.zhipin.com/web/geek/jobs'}]);
assert.equal(await valid.context.resolveTabId(undefined, 'boss'), 1);
const inspected = await valid.context.handleCommand({id: 'inspect', action: 'inspect', workspace: 'boss'});
assert.equal(inspected.ok, true);
assert.equal(inspected.data.host, 'www.zhipin.com');
assert.equal(inspected.data.transport, 'isolated-content-script');
assert.equal(valid.requests.length, 0);
const fetched = await valid.context.handleCommand({id: 'fetch', action: 'fetch', url: 'https://www.zhipin.com/wapi/zpuser/wap/getUserInfo.json', workspace: 'boss'});
assert.equal(fetched.ok, true);
assert.equal(fetched.data.code, 0);
assert.equal(fetched.data.zpData.mid, '9007199254740993');
assert.equal(valid.requests.length, 1);
assert.equal(valid.requests[0].options.credentials, 'include');
assert.equal(valid.requests[0].options.redirect, 'error');
assert.equal(valid.requests[0].options.method, 'GET');
assert.equal(valid.requests[0].options.headers.Referer, undefined);

const contextTabs = [{id: 80, url: 'https://www.zhipin.com/web/geek/chat', active: true}, {id: 81, url: 'https://www.zhipin.com/web/geek/jobs', active: false}];
const contextChoice = runtime(contextTabs);
await contextChoice.context.handleCommand({id: 'chat-inspect', action: 'inspect', workspace: 'boss'});
const wrongPage = await contextChoice.context.handleCommand({id: 'wrong-context', action: 'fetch', workspace: 'boss', url: 'https://www.zhipin.com/wapi/zpgeek/search/joblist.json'});
assert.equal(wrongPage.data.code, 'BRIDGE_SEARCH_PAGE_REQUIRED');
assert.equal(wrongPage.data._bridge_meta.request_sent, false);
assert.equal(wrongPage.data._bridge_meta.page_path, '/web/geek/chat');
assert.equal(contextChoice.requests.length, 0);
contextTabs[0].active = false;
contextTabs[1].active = true;
const newlySelected = await contextChoice.context.handleCommand({id: 'manual-selection', action: 'inspect', workspace: 'boss'});
assert.equal(newlySelected.data.path, '/web/geek/jobs');
assert.equal(contextChoice.injections.at(-1).target.tabId, 81);
contextTabs[0].active = true;
contextTabs[1].active = false;
assert.equal(await contextChoice.context.resolveTabId(undefined, 'boss'), 81);
vm.runInContext('activeCommands = 2', contextChoice.context);
await contextChoice.context.handleCommand({id: 'concurrent-inspection', action: 'inspect', workspace: 'boss'});
assert.equal(contextChoice.injections.at(-1).target.tabId, 81);
vm.runInContext('activeCommands = 0', contextChoice.context);
assert.equal(contextChoice.requests.length, 0);

const greeting = {greeting: 'Fixture & text', jobId: 'fixture-id'};

const permissionFailure = runtime([{id: 71, url: 'https://www.zhipin.com/web/geek/chat', status: 'complete'}]);
permissionFailure.chrome.scripting.executeScript = async () => {throw new Error('Cannot access contents of the page. Extension manifest must request permission to access the respective host.');};
const notExecuted = await permissionFailure.context.handleCommand({id: 'permission', action: 'fetch', workspace: 'boss', method: 'POST', url: 'https://www.zhipin.com/wapi/zpgeek/friend/add.json'});
assert.equal(notExecuted.code, 'BROWSER_PAGE_ACCESS_DENIED');
assert.equal(notExecuted.stage, 'before_injection');
assert.equal(notExecuted.executed, false);
assert.equal(permissionFailure.requests.length, 0);

const loadingTab = {id: 72, url: 'https://www.zhipin.com/web/geek/chat', status: 'loading'};
const loading = runtime([loadingTab]);
loading.context.setTimeout = callback => {assert.equal(loading.requests.length, 0); loadingTab.status = 'complete'; callback(); return 1;};
const afterLoad = await loading.context.handleCommand({id: 'loaded', action: 'fetch', workspace: 'boss', url: 'https://www.zhipin.com/wapi/test'});
assert.equal(afterLoad.ok, true);
assert.equal(loading.requests.length, 1);

const lostReply = runtime([{id: 73, url: 'https://www.zhipin.com/web/geek/chat'}]);
const runThenLose = lostReply.chrome.scripting.executeScript;
lostReply.chrome.scripting.executeScript = async options => {await runThenLose(options); return [];};
const unknownResult = await lostReply.context.handleCommand({id: 'lost', action: 'fetch', workspace: 'boss', method: 'POST', url: 'https://www.zhipin.com/wapi/zpgeek/friend/add.json'});
assert.equal(unknownResult.ok, false);
assert.equal(unknownResult.executed, null);
assert.equal(lostReply.requests.length, 1);

const sleepingTab = runtime([{id: 74, url: 'https://www.zhipin.com/web/geek/chat', discarded: true}]);
const sleepingResult = await sleepingTab.context.handleCommand({id: 'sleeping', action: 'fetch', workspace: 'boss', url: 'https://www.zhipin.com/wapi/test'});
assert.equal(sleepingResult.code, 'BROWSER_PAGE_DISCARDED');
assert.equal(sleepingResult.executed, false);
assert.equal(sleepingTab.injections.length, 0);

const posted = await valid.context.handleCommand({id: 'post', action: 'fetch', method: 'POST', url: 'https://www.zhipin.com/wapi/zpgeek/friend/add.json', data: greeting, workspace: 'boss'});
assert.equal(posted.ok, true);
assert.equal(new URLSearchParams(valid.requests[1].options.body).get('greeting'), greeting.greeting);
valid.context.document.body.innerText = '环境存在异常';
const ordinaryWords = await valid.context.handleCommand({id: 'normal-content', action: 'inspect', workspace: 'boss'});
assert.equal(ordinaryWords.data.verificationVisible, false);
const prompt = {innerText: '请完成安全验证', closest: () => null, getClientRects: () => [1], getBoundingClientRect: () => ({width: 500, height: 200, top: 0, left: 0, right: 500, bottom: 200})};
valid.context.document.querySelectorAll = selector => selector.startsWith('dialog') ? [prompt] : [];
const blocked = await valid.context.handleCommand({id: 'blocked', action: 'fetch', method: 'POST', url: 'https://www.zhipin.com/wapi/zpgeek/friend/add.json', data: greeting, workspace: 'boss'});
assert.equal(blocked.data.code, 'BRIDGE_PAGE_VERIFICATION');
assert.equal(blocked.data._bridge_meta.source, 'page_verification');
assert.equal(blocked.data._bridge_meta.request_sent, false);
assert.equal(valid.requests.length, 2);
valid.context.document.querySelectorAll = () => [];
const normalText = runtime([{id: 80, url: 'https://www.zhipin.com/web/geek/chat'}]);
normalText.context.document.body.innerText = '聊天提到安全验证，JD要求开发访问验证功能，文档中包含环境存在异常的报错示例。';
const normalRead = await normalText.context.handleCommand({id: 'normal-read', action: 'fetch', workspace: 'boss', url: 'https://www.zhipin.com/wapi/test'});
assert.equal(normalRead.data._bridge_meta.source, 'api_response');
assert.equal(normalRead.data._bridge_meta.verification.detected, false);
assert.equal(normalText.requests.length, 1);
normalText.context.document.querySelectorAll = selector => selector.startsWith('dialog') ? [prompt] : [];
normalText.context.getComputedStyle = () => ({display: 'none', visibility: 'visible', opacity: '1'});
assert.equal((await normalText.context.handleCommand({id: 'hidden', action: 'inspect', workspace: 'boss'})).data.verificationVisible, false);
const serverRejection = runtime([{id: 81, url: 'https://www.zhipin.com/web/geek/jobs'}]);
let remoteCalls = 0;
serverRejection.context.fetch = async () => {remoteCalls++; return {status: 200, text: async () => JSON.stringify({code: 37, message: '环境存在异常', _bridge_meta: {source: 'page_verification', request_sent: false}})};};
const rejected = await serverRejection.context.handleCommand({id: 'remote-code37', action: 'fetch', workspace: 'boss', url: 'https://www.zhipin.com/wapi/test'});
assert.equal(rejected.data.code, 37);
assert.equal(rejected.data._bridge_meta.source, 'api_response');
assert.equal(rejected.data._bridge_meta.request_sent, true);
assert.equal(rejected.data._bridge_meta.verification.detected, false);
assert.equal(remoteCalls, 1);
const loginPage = runtime([{id: 82, url: 'https://www.zhipin.com/web/user/'}]);
const loginBlocked = await loginPage.context.handleCommand({id: 'login-page', action: 'fetch', workspace: 'boss', url: 'https://www.zhipin.com/wapi/test'});
assert.equal(loginBlocked.data.code, 'BRIDGE_LOGIN_REQUIRED');
assert.equal(loginBlocked.data._bridge_meta.source, 'page_login');
assert.equal(loginPage.requests.length, 0);
const chat = runtime([{id: 91, url: 'https://www.zhipin.com/web/geek/chat'}]);
const probe = await chat.context.handleCommand({id: 'read-chat', action: 'inspect-chat', workspace: 'boss', data: {company: 'Fixture Company', job_id: 'fixture-job'}});
assert.equal(probe.ok, true);
assert.equal(probe.data.contactCount, 0);
assert.equal(probe.data.input, null);
assert.equal(chat.requests.length, 0);
const wrongTarget = await chat.context.handleCommand({id: 'not-current', action: 'send-chat', workspace: 'boss', data: {confirm: true, peer_id: '22', job_id: 'fixture-job', text: 'Approved text'}});
assert.equal(wrongTarget.ok, true);
assert.equal(wrongTarget.data.submitted, false);
assert.equal(wrongTarget.data.stage, 'before_submit');
assert.equal(chat.requests.length, 0);
assert.equal((await chat.context.handleCommand({id: 'no-confirm', action: 'reload'})).ok, false);
assert.equal(chat.reloadCount(), 0);
chat.context.setTimeout = (callback, delay) => {assert.equal(delay, 100); callback(); return 1;};
assert.equal((await chat.context.handleCommand({id: 'update', action: 'reload', data: {confirm: true}})).ok, true);
assert.equal(chat.reloadCount(), 1);
valid.context.document.body.innerText = 'Fixture content';
for (const command of [
	{action: 'exec', code: 'window.close()'},
	{action: 'fetch', url: 'https://evil.example/wapi/test'},
	{action: 'fetch', method: 'POST', url: 'https://www.zhipin.com/wapi/resume/upload.json'},
	{action: 'fetch', method: 'DELETE', url: 'https://www.zhipin.com/wapi/test'},
	{action: 'fetch', url: 'https://www.zhipin.com/wapi/test', data: []},
]) {
	assert.equal((await valid.context.handleCommand({id: 'denied', workspace: 'boss', ...command})).ok, false);
}
assert.equal(valid.requests.length, 2);
assert.equal((await valid.context.handleCloseWindow({id: 'preserve'})).data.preserved, true);
await assert.rejects(valid.context.handleNavigate({workspace: 'boss', url: 'https://example.com'}), /not allowed/);
assert.equal((await valid.context.handleCookies({id: 'test', domain: 'zhipin.com'})).ok, false);
const invalid = runtime([{id: 9, url: 'https://zhipin.com.evil.example/'}]);
await assert.rejects(invalid.context.resolveTabId(9, 'boss'), /not a BOSS page/);
assert.equal(valid.context.isBossUrl('https://zhipin.com.evil.example/'), false);
const manifest = JSON.parse(fs.readFileSync(new URL('./manifest.json', import.meta.url), 'utf8'));
assert.ok(!manifest.permissions.includes('cookies'));
assert.ok(!manifest.permissions.includes('debugger'));
assert.ok(manifest.permissions.includes('scripting'));
assert.ok(!source.includes('chrome.debugger'));
assert.ok(!source.includes('Runtime.enable'));
assert.ok(!source.includes('eval('));
const popup = fs.readFileSync(new URL('./popup.html', import.meta.url), 'utf8');
assert.ok(popup.includes('<script src="popup.js"></script>'));
assert.ok(!popup.includes('<script>'));
console.log('Extension safety checks passed: fixed isolated scripts, preserved tabs, typed requests, verification stop, no debugger, no arbitrary code, no cookie export.');
