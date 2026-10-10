/**
 * BOSS Agent Bridge — Service Worker (background script).
 *
 * Connects to boss-agent-cli daemon via WebSocket, receives commands,
 * Runs fixed, isolated content-script operations in an existing BOSS tab.
 */

const DAEMON_PORT = 19826;
const DAEMON_WS_URL = `ws://127.0.0.1:${DAEMON_PORT}/ext`;
const DAEMON_PING_URL = `http://127.0.0.1:${DAEMON_PORT}/ping`;

let ws = null;
let reconnectTimer = null;
let reconnectAttempts = 0;
const MAX_EAGER_ATTEMPTS = 6;
let selectedBossTabId = null;
let activeCommands = 0;
let chatActionBusy = false;

// One fixed dispatcher keeps every page action on the same self-contained verification check.
async function pageCommand(action, request = {}) {
	function detectVerification() {
		const evidence = [];
		if (/\/(?:verify|captcha|security-check|security_check|security)(?:\/|$)/i.test(location.pathname)) {
			evidence.push({kind: 'verification_path', cue: location.pathname});
		}
		const visible = element => {
			if (!element.getClientRects().length || element.closest('[hidden],[aria-hidden="true"]')) return false;
			const style = getComputedStyle(element);
			const rect = element.getBoundingClientRect();
			return style.display !== 'none' && style.visibility !== 'hidden' && Number(style.opacity) !== 0 &&
				rect.width > 0 && rect.height > 0 && rect.bottom > 0 && rect.right > 0 && rect.top < innerHeight && rect.left < innerWidth;
		};
		const prompts = /请(?:先)?(?:完成|通过|进行).{0,10}验证|拖动.{0,8}滑块|滑动.{0,8}验证|完成.{0,8}拼图|环境存在异常|访问过于频繁/;
		if (/^(?:BOSS直聘[\s|\-]*)?(?:安全验证|人机验证|访问验证)(?:[\s|\-]*BOSS直聘)?$/.test(document.title.trim()) && prompts.test(document.body?.innerText || '')) {
			evidence.push({kind: 'visible_verification_prompt', cue: '验证页标题及提示'});
		}
		const selectors = 'dialog[open],[role="dialog"],[role="alertdialog"],[role="alert"],[id*="captcha"],[class*="captcha"],[id*="geetest"],[class*="geetest"],[id*="verify"],[class*="verify"]';
		for (const element of document.querySelectorAll(selectors)) {
			if (!visible(element)) continue;
			const match = String(element.innerText || '').match(prompts);
			if (match) evidence.push({kind: 'visible_verification_prompt', cue: match[0]});
			if (evidence.length >= 3) break;
		}
		for (const frame of document.querySelectorAll('iframe[src*="captcha"],iframe[src*="geetest"],iframe[src*="/verify"]')) {
			if (!visible(frame)) continue;
			const rect = frame.getBoundingClientRect();
			if (rect.width >= 160 && rect.height >= 80) evidence.push({kind: 'visible_verification_frame', cue: 'verification iframe'});
		}
		return {detected: evidence.length > 0, evidence: evidence.slice(0, 3)};
	}

	function pageBlock(verification) {
		return {code: 'ENVIRONMENT_RISK', _bridge_meta: {source: 'page_verification', request_sent: false, verification}};
	}

	function inspectPage() {
		const verification = detectVerification();
		return {
			host: location.hostname, path: location.pathname,
			href: location.origin + location.pathname, title: document.title,
			verificationVisible: verification.detected, verification,
			fetchAvailable: typeof fetch === 'function', transport: 'isolated-content-script',
			chatReady: !!document.querySelector('li[role="listitem"] .friend-content'),
			documentStamp: performance.timeOrigin,
		};
	}

	function inspectSearch() {
		const element = document.querySelector('.page-jobs-main');
		const vm = element?.__vue__ || element?.__vueParentComponent?.proxy;
		const fields = value => value && typeof value === 'object' ? Object.keys(value).slice(0, 80) : [];
		const list = Array.isArray(vm?.jobList) ? vm.jobList : [];
		const parameters = value => Object.fromEntries(['query', 'city', 'experience', 'degree', 'jobType', 'salary', 'page', 'pageSize'].filter(key => typeof value?.[key] === 'string' || typeof value?.[key] === 'number' || Array.isArray(value?.[key])).map(key => [key, value[key]]));
		const filter = document.querySelector('.filter-condition-inner');
		const filterVm = filter?.__vue__ || filter?.__vueParentComponent?.proxy;
		return {
			...inspectPage(), componentPresent: !!vm, componentKeys: fields(vm?.$data), methods: fields(vm?.$options?.methods),
			listCount: list.length, listFields: fields(list[0]), route: parameters(vm?.$route?.query),
			parameters: parameters(vm?.formData), formFields: fields(vm?.formData), paginationFields: fields(vm?.pageVo), pagination: parameters(vm?.pageVo),
			listState: Object.fromEntries(['page', 'hasMore', 'loading', 'isLoading'].filter(key => typeof vm?.[key] === 'boolean' || typeof vm?.[key] === 'number').map(key => [key, vm[key]])),
			filterKeys: fields(filterVm?.$data), filters: (filterVm?.$children || []).map(child => ({placeholder: child.placeholder || '', dataKeys: fields(child.$data), propKeys: fields(child.$props)})),
			cards: document.querySelectorAll('.job-card-wrap,.job-card-box').length,
			cardStructure: [...document.querySelectorAll('.job-card-box')].slice(0, 1).map(card => ({tag: card.tagName, classes: card.className, children: [...card.children].map(child => ({tag: child.tagName, classes: child.className})), props: fields(card.__vue__?.$props), data: fields(card.__vue__?.$data)})),
			detailFields: fields(vm?.jobDetail), jobInfoFields: fields(vm?.jobDetail?.jobInfo),
			visibility: document.visibilityState,
			jobSamples: list.slice(0, 3).map(job => pickSearchJob(job)),
			nativeCapture: globalThis.__bossAgentNativeCapture ? {started: globalThis.__bossAgentNativeCapture.started.map(entry => ({path: entry.path, params: Object.fromEntries(Object.entries(entry.params).filter(([key]) => key !== 'securityId')), at: entry.at})), responses: globalThis.__bossAgentNativeCapture.entries.map(entry => ({path: entry.path, params: Object.fromEntries(Object.entries(entry.params).filter(([key]) => key !== 'securityId')), at: entry.at, status: entry.status, code: entry.body.code, listCount: entry.body.zpData?.jobList?.length}))} : null,
		};
		function pickSearchJob(job) {return {title: job.jobName, company: job.brandName, salary: job.salaryDesc, city: job.cityName};}
	}

	function collectNative(request) {
		const verification = detectVerification();
		if (request.phase !== 'read' && verification.detected) return {...pageBlock(verification), code: 'BRIDGE_PAGE_VERIFICATION'};
		if (location.origin !== 'https://www.zhipin.com' || !/^\/web\/geek\/jobs?\/?$/.test(location.pathname)) {
			if (request.phase === 'read') return {code: 'BRIDGE_NATIVE_PAGE_CHANGED', message: '读取期间网页已改变，结果未确认。', _bridge_meta: {source: 'page_context', transport: 'native-page', request_sent: null, page_path: location.pathname}};
			return {code: 'BRIDGE_SEARCH_PAGE_REQUIRED', _bridge_meta: {source: 'page_context', request_sent: false, page_path: location.pathname}};
		}
		const element = document.querySelector('.page-jobs-main');
		const component = () => element?.__vue__ || element?.__vueParentComponent?.proxy;
		const vm = component();
		const fail = (code, message, sent = false) => ({code, message, _bridge_meta: {source: 'page_context', transport: 'native-page', request_sent: sent, page_path: location.pathname}});
		if (!vm || !Array.isArray(vm.jobList)) return fail('BRIDGE_NATIVE_PAGE_UNAVAILABLE', '网页岗位组件尚未就绪，未发起采集。');
		if (request.phase !== 'read' && request.deadline && Date.now() >= request.deadline) return fail('BRIDGE_NATIVE_READ_TIMEOUT', '网页操作未在截止前开始，未追加请求。');
		const scalar = value => Array.isArray(value) ? value.map(String).sort().join(',') : String(value ?? '');
		const matches = (actual, expected) => Object.entries(expected).every(([key, value]) => scalar(actual[key]) === scalar(value));
		const listFields = ['securityId', 'encryptJobId', 'encryptBossId', 'bossName', 'bossTitle', 'jobName', 'lid', 'salaryDesc', 'jobLabels', 'skills', 'jobExperience', 'jobDegree', 'cityName', 'areaDistrict', 'businessDistrict', 'jobType', 'city', 'brandName', 'brandStageName', 'brandIndustry', 'brandScaleName', 'welfareList', 'daysPerWeekDesc', 'leastMonthDesc'];
		const pick = (value, keys) => Object.fromEntries(keys.filter(key => value?.[key] !== undefined).map(key => [key, value[key]]));
		let capture = globalThis.__bossAgentNativeCapture;
		if (!capture) {
			// Observe only the site's own job reads. Never inspect cookies, headers or verification seeds.
			capture = {entries: [], started: [], jobs: new Map()};
			const allowed = new Set(['/wapi/zpgeek/search/joblist.json', '/wapi/zpgeek/pc/recommend/job/list.json', '/wapi/zpgeek/job/detail.json']);
			const describe = value => {
				try {
					const url = new URL(value, location.href);
					if (url.origin !== location.origin || !allowed.has(url.pathname)) return null;
					const keys = ['query', 'city', 'experience', 'degree', 'salary', 'jobType', 'industry', 'scale', 'page', 'securityId'];
					return {path: url.pathname, params: Object.fromEntries(keys.map(key => [key, url.searchParams.get(key) || ''])), at: performance.now()};
				} catch { return null; }
			};
			const remember = jobs => {
				for (const job of jobs || []) if (job?.securityId && job.encryptJobId) capture.jobs.set(String(job.securityId), pick(job, listFields));
				while (capture.jobs.size > 300) capture.jobs.delete(capture.jobs.keys().next().value);
			};
			const record = (meta, data, status) => {
				if (!meta || !data || typeof data !== 'object' || Array.isArray(data)) return;
				const body = {code: data.code, message: typeof data.message === 'string' ? data.message.slice(0, 300) : typeof data.msg === 'string' ? data.msg.slice(0, 300) : ''};
				if (data.code === 0 && Array.isArray(data.zpData?.jobList)) {
					body.zpData = {jobList: data.zpData.jobList.map(job => pick(job, listFields)), hasMore: data.zpData.hasMore};
					remember(body.zpData.jobList);
				}
				capture.entries.push({...meta, body, status});
				if (capture.entries.length > 12) capture.entries.shift();
			};
			const parse = text => JSON.parse(text, (_key, value, context) => {
				if (typeof value === 'number' && Number.isInteger(value) && !Number.isSafeInteger(value)) {
					if (!context?.source) throw new Error('Exact identifier unavailable');
					return context.source;
				}
				return value;
			});
			const started = meta => {if (meta) {capture.started.push(meta); if (capture.started.length > 12) capture.started.shift();}};
			const xhrRequests = new WeakMap();
			const open = XMLHttpRequest.prototype.open;
			const send = XMLHttpRequest.prototype.send;
			XMLHttpRequest.prototype.open = function(method, url, ...args) {
				xhrRequests.set(this, String(method).toUpperCase() === 'GET' ? describe(url) : null);
				return open.call(this, method, url, ...args);
			};
			XMLHttpRequest.prototype.send = function(...args) {
				const meta = xhrRequests.get(this);
				if (meta) {
					meta.at = performance.now();
					this.addEventListener('load', () => {try {record(meta, this.responseType === 'json' ? this.response : parse(this.responseText), this.status);} catch {}}, {once: true});
				}
				const result = send.apply(this, args);
				started(meta);
				return result;
			};
			const nativeFetch = globalThis.fetch;
			globalThis.fetch = async function(resource, options) {
				const meta = String(options?.method || resource?.method || 'GET').toUpperCase() === 'GET' ? describe(typeof resource === 'string' || resource instanceof URL ? resource : resource?.url) : null;
				started(meta);
				const response = await nativeFetch.call(this, resource, options);
				if (meta) response.clone().text().then(text => {try {record(meta, parse(text), response.status);} catch {}}).catch(() => {});
				return response;
			};
			Object.defineProperty(globalThis, '__bossAgentNativeCapture', {value: capture, configurable: false, writable: false});
		}
		for (const job of vm.jobList) if (job?.securityId && job.encryptJobId) capture.jobs.set(String(job.securityId), pick(job, listFields));
		while (capture.jobs.size > 300) capture.jobs.delete(capture.jobs.keys().next().value);
		let began = performance.now();
		let stamp = performance.timeOrigin;
		const nativeMeta = (source, sent, entry) => ({source, transport: 'native-page', request_sent: sent, endpoint: entry?.path || '', http_status: entry?.status, page_path: location.pathname, verification: detectVerification()});
		let expected;
		let job;
		let restorePage = 0;
		let baselineList = vm.jobList;
		let detailAlreadyReady = false;
		const deadline = request.deadline || Date.now() + 20000;
		const finish = result => {capture.operation = null; return result;};
		if (request.phase === 'read') {
			const operation = capture.operation;
			if (!operation || operation.id !== request.operationId) return fail('BRIDGE_NATIVE_PAGE_CHANGED', '网页采集状态已经变化，未追加请求。', null);
			({began, stamp, expected, job, restorePage, baselineList, detailAlreadyReady} = operation);
			if (Date.now() >= deadline) return finish(fail('BRIDGE_NATIVE_READ_TIMEOUT', '网页采集超时，未确认结果；没有自动重复请求。', capture.started.some(entry => entry.at >= began) ? true : null));
		} else if (request.kind === 'search') {
			const params = request.params || {};
			const keys = ['query', 'city', 'experience', 'degree', 'salary', 'jobType', 'industry', 'scale'];
			if (!params.query || typeof params.query !== 'string' || params.query.length > 100 || !/^\d{9}$/.test(String(params.city)) || !Number.isInteger(params.page) || params.page < 1) return fail('BRIDGE_NATIVE_PARAMETERS', '网页采集参数无效。');
			if (Object.keys(params).some(key => ![...keys, 'page'].includes(key))) return fail('BRIDGE_NATIVE_PARAMETERS', '网页采集不支持此搜索参数，未发起请求。');
			expected = Object.fromEntries(keys.map(key => [key, params[key] ?? '']));
			if (params.page === 1 || !matches(vm.formData || {}, expected)) {
				if (typeof vm.onSearchChange !== 'function' || typeof vm.onFilterChange !== 'function') return fail('BRIDGE_NATIVE_PAGE_UNAVAILABLE', '网页搜索与筛选控件尚未就绪。');
				const form = {...expected, city: Number(params.city), experience: params.experience ? scalar(params.experience).split(',') : [], degree: params.degree ? scalar(params.degree).split(',') : []};
				// The keyword control establishes search mode; the filter control alone resets query in recommendation mode.
				vm.onSearchChange({query: params.query});
				vm.onFilterChange(form);
				if (params.page > 1) restorePage = params.page;
			} else {
				if (vm.hasMore === false) return {code: 0, zpData: {jobList: [], hasMore: false}, _bridge_meta: nativeMeta('page_state', false)};
				if (vm.loading || vm.moreLoading || typeof vm.onSearch !== 'function') return fail('BRIDGE_NATIVE_PAGE_UNAVAILABLE', '网页仍在加载，未重复请求。');
				vm.onSearch(params.page, {loadMore: true});
			}
			expected.page = restorePage ? 1 : params.page;
		} else if (request.kind === 'detail') {
			job = capture.jobs.get(String(request.params?.securityId));
			const saved = request.params?.nativeJob;
			if (saved && /^[A-Za-z0-9_-]{1,128}$/.test(String(saved.encryptJobId || '')) && typeof saved.securityId === 'string' && saved.securityId === request.params.securityId) {
				const current = [...capture.jobs.values()].find(value => value.encryptJobId === saved.encryptJobId);
				job = current || pick(saved, listFields);
			}
			if (!job || typeof vm.clickJobCardAction !== 'function') return fail('BRIDGE_NATIVE_JOB_UNAVAILABLE', '此岗位不在当前网页采集记录中，请保留搜索页后继续采集。');
			detailAlreadyReady = vm.currentJob?.encryptJobId === job.encryptJobId && !vm.jobDetailLoading && vm.jobDetail?.jobInfo?.encryptId === job.encryptJobId && !!vm.jobDetail?.jobInfo?.postDescription;
			if (vm.currentJob?.encryptJobId !== job.encryptJobId) vm.clickJobCardAction(job);
			else if (vm.jobDetail?.jobInfo?.encryptId !== job.encryptJobId || !vm.jobDetail?.jobInfo?.postDescription) return fail('BRIDGE_NATIVE_DETAIL_UNAVAILABLE', '网页尚未加载此岗位的完整JD，未重复请求。');
		} else return fail('BRIDGE_NATIVE_PARAMETERS', 'Unknown native collection kind');
		if (request.phase !== 'read') capture.operation = {id: request.operationId, began, stamp, expected, job, restorePage, baselineList, detailAlreadyReady, deadline};
			if (performance.timeOrigin !== stamp || !element.isConnected || component() !== vm || !/^\/web\/geek\/jobs?\/?$/.test(location.pathname)) return fail('BRIDGE_NATIVE_PAGE_CHANGED', '采集中网页发生变化，已保留进度。', null);
			const belongs = entry => entry.at >= began && (expected ? entry.path !== '/wapi/zpgeek/job/detail.json' && matches(entry.params, expected) : entry.path === '/wapi/zpgeek/job/detail.json' && entry.params.securityId === String(job.securityId));
			let entry = capture.entries.find(belongs);
			if (entry && entry.body.code !== 0) return finish({...entry.body, _bridge_meta: nativeMeta('api_response', true, entry)});
			if (entry && (entry.status < 200 || entry.status >= 300)) return finish({code: 'BRIDGE_NATIVE_HTTP_ERROR', message: `网页读取返回HTTP ${entry.status}，未接受此结果。`, _bridge_meta: nativeMeta('api_response', true, entry)});
			const currentVerification = detectVerification();
			if (currentVerification.detected) {
				const sent = capture.started.some(belongs);
				return finish({code: 'BRIDGE_PAGE_VERIFICATION', _bridge_meta: {...nativeMeta('page_verification', sent), verification: currentVerification}});
			}
			// Some site transports retain their own XHR implementation; accept only a newly committed, nonempty page list.
			if (!entry && expected && vm.jobList !== baselineList && !vm.loading && !vm.moreLoading && vm.pageVo?.page === expected.page && matches(vm.formData || {}, Object.fromEntries(Object.entries(expected).filter(([key]) => key !== 'page'))) && vm.jobList.length > 0) {
				let rows = vm.jobList;
				if (expected.page > 1 && baselineList.length && baselineList.every((value, index) => rows[index]?.encryptJobId === value.encryptJobId)) rows = rows.slice(baselineList.length);
				if (rows.length && rows.every(value => typeof value.encryptJobId === 'string' && value.encryptJobId && typeof value.securityId === 'string' && value.securityId)) {
					entry = {path: '', body: {code: 0, zpData: {jobList: rows.map(value => pick(value, listFields)), hasMore: vm.hasMore}}, pageState: true};
				}
			}
			if (expected && entry?.body.zpData) {
				if (vm.loading || vm.moreLoading || entry.body.zpData.jobList.some(job => !vm.jobList.some(value => value.encryptJobId === job.encryptJobId))) {
					return {_native_pending: true};
				}
				const current = vm.formData || {};
				if (!matches(current, Object.fromEntries(Object.entries(expected).filter(([key]) => key !== 'page')))) return fail('BRIDGE_NATIVE_PAGE_CHANGED', '搜索条件被修改，未使用不匹配的结果。', true);
				if (restorePage) {
					if (vm.hasMore === false) return {code: 0, zpData: {jobList: [], hasMore: false}, _bridge_meta: nativeMeta('api_response', true, entry)};
					if (vm.loading || vm.moreLoading || typeof vm.onSearch !== 'function') return fail('BRIDGE_NATIVE_PAGE_UNAVAILABLE', '网页条件恢复后尚未就绪，未重复请求。', true);
					expected.page = restorePage;
					restorePage = 0;
					began = performance.now();
					baselineList = vm.jobList;
					Object.assign(capture.operation, {expected, restorePage, began, baselineList});
					vm.onSearch(expected.page, {loadMore: true});
					return {_native_pending: true};
				}
				return finish({...entry.body, _bridge_meta: nativeMeta(entry.pageState ? 'page_state' : 'api_response', entry.pageState ? null : true, entry)});
			}
			if (job && vm.currentJob?.encryptJobId === job.encryptJobId && !vm.jobDetailLoading && vm.jobDetail?.jobInfo?.encryptId === job.encryptJobId && vm.jobDetail.jobInfo.postDescription) {
				const detail = vm.jobDetail, info = detail.jobInfo, boss = detail.bossInfo || {};
				const city = typeof info.locationName === 'string' && info.locationName ? (job.cityName && info.locationName.startsWith(job.cityName) ? job.cityName : info.locationName) : job.cityName;
				const card = {...pick(info, ['jobName', 'salaryDesc', 'experienceName', 'degreeName', 'jobType', 'postDescription', 'address', 'payTypeDesc']), encryptJobId: job.encryptJobId, brandName: detail.brandComInfo?.brandName || job.brandName, cityName: city, jobLabels: job.skills || job.jobLabels || [], bossName: boss.name, bossTitle: boss.title, activeTimeDesc: boss.activeTimeDesc, daysPerWeekDesc: job.daysPerWeekDesc, leastMonthDesc: job.leastMonthDesc};
				return finish({code: 0, zpData: {jobCard: card}, _bridge_meta: nativeMeta(entry ? 'api_response' : 'page_state', entry ? true : detailAlreadyReady ? false : null, entry)});
			}
		return {_native_pending: true};
	}

	function canRefreshChat() {
		const fail = error => ({ok: false, submitted: false, stage: 'before_submit', error});
		if (location.origin !== 'https://www.zhipin.com' || location.pathname !== '/web/geek/chat') return fail('BOSS chat page required');
		const verification = detectVerification();
		if (verification.detected) return {...fail('BOSS verification component detected'), ...pageBlock(verification)};
		if (document.querySelector('#chat-input')?.textContent.trim()) return fail('An existing draft will not be overwritten');
		if (document.querySelector('input.boss-search-input')?.value.trim()) return fail('An existing contact search will not be overwritten');
		return {ok: true, href: location.href, documentStamp: performance.timeOrigin};
	}

	function inspectChat(expected) {
		if (location.hostname !== 'www.zhipin.com' || location.pathname !== '/web/geek/chat') throw new Error('Open the existing BOSS chat page first');
		const normalize = value => String(value || '').normalize('NFKC').replace(/\s+/g, '');
		const company = normalize(expected.company);
		if (!company) throw new Error('A specific target company is required');
		const describe = element => ({tag: element.tagName, id: element.id, classes: element.className, role: element.getAttribute('role'), attributes: element.getAttributeNames().filter(name => name.startsWith('data-'))});
		const input = document.querySelector('#chat-input');
		const ancestry = [];
		for (let element = input?.parentElement; element && ancestry.length < 5; element = element.parentElement) {
			ancestry.push({...describe(element), children: [...element.children].slice(0, 16).map(describe)});
		}
		const rows = [...document.querySelectorAll('li[role="listitem"]')];
		const targets = rows.filter(row => normalize(row.innerText).includes(company));
		const links = [...document.querySelectorAll('a[href*="/job_detail/"]')].filter(link => link.getClientRects().length).map(link => {
			const url = new URL(link.href);
			return {path: url.pathname, matchesJob: url.pathname === `/job_detail/${expected.job_id}.html`, text: link.innerText.slice(0, 100)};
		});
		return {
			path: location.pathname, verificationVisible: detectVerification().detected,
			headerText: document.querySelector('.chat-conversation .top-info-content')?.innerText.slice(0, 700) || '',
			headerChildren: [...(document.querySelector('.chat-conversation .top-info-content')?.children || [])].map(describe),
			input: input ? {...describe(input), editable: input.isContentEditable, draftLength: input.innerText.length} : null,
			sendButtons: [...document.querySelectorAll('.btn-send')].map(button => ({...describe(button), text: button.innerText, disabled: button.disabled || button.classList.contains('disabled') || button.classList.contains('is-disabled')})),
			contactCount: rows.length,
			contactSearchInputs: [...document.querySelectorAll('input')].filter(element => /联系人/.test(element.getAttribute('placeholder') || '')).map(element => ({...describe(element), placeholder: element.getAttribute('placeholder'), valueLength: element.value.length, visible: !!element.getClientRects().length})),
			matchingContacts: targets.map(row => ({...describe(row), text: row.innerText.slice(0, 250), children: [...row.children].slice(0, 12).map(describe), descendants: [...row.querySelectorAll('*')].slice(0, 16).map(describe)})),
			jobLinks: links, ancestry,
		};
	}

	function inspectChatIdentity(expected) {
		if (location.hostname !== 'www.zhipin.com' || location.pathname !== '/web/geek/chat') throw new Error('BOSS chat page required');
		const summary = value => {
			if (!value || typeof value !== 'object') return {present: false};
			const keys = Object.keys(value).slice(0, 60);
			const matches = {};
			for (const key of keys.filter(key => /^(?:uid|id|friendId|bossId|userId|encryptUid|jobId|encryptJobId)$/i.test(key))) {
				matches[key] = {peer: String(value[key]) === String(expected.peer_id), job: String(value[key]) === String(expected.job_id), type: typeof value[key]};
			}
			return {present: true, keys, matches};
		};
		const results = ['.chat-editor', '.chat-conversation'].map(selector => {
			const element = document.querySelector(selector);
			const vm = element?.__vue__ || element?.__vueParentComponent?.proxy;
			return {selector, found: !!element, hasComponent: !!vm, disabled: typeof vm?.disabled === 'boolean' ? vm.disabled : null, data: summary(vm?.$data), props: summary(vm?.$props), computedKeys: Object.keys(vm?.$options?.computed || {}), conversation: summary(vm?.conversation$)};
		});
		const company = String(expected.company || '').normalize('NFKC').replace(/\s+/g, '');
		for (const row of [...document.querySelectorAll('li[role="listitem"]')].filter(row => company && row.innerText.normalize('NFKC').replace(/\s+/g, '').includes(company))) {
			const element = row.querySelector('.friend-content-warp');
			const instance = element?.__vueParentComponent;
			const vm = element?.__vue__ || instance?.proxy;
			results.push({selector: 'matching-contact', hasComponent: !!vm, ownKeys: vm ? Object.keys(vm).slice(0, 60) : [], conversation: summary(vm?.conversation$), props: summary(instance?.props), propValues: Object.fromEntries(Object.entries(instance?.props || {}).map(([key, value]) => [key, summary(value)])), setupKeys: Object.keys(instance?.setupState || {}).slice(0, 60)});
		}
		return results;
	}

	async function chatAction(request, phase) {
		let submitted = false;
		const fail = error => ({ok: false, submitted, stage: submitted ? 'after_submit' : 'before_submit', error});
		try {
			if (location.origin !== 'https://www.zhipin.com' || location.pathname !== '/web/geek/chat') return fail('BOSS chat page required');
			if (request.confirm !== true || !/^[1-9][0-9]*$/.test(String(request.peer_id || '')) || typeof request.job_id !== 'string' || !request.job_id || typeof request.text !== 'string' || !request.text.trim() || request.text.length > 300) return fail('Explicit recipient, job and approved text required');
			const normalize = value => String(value || '').normalize('NFKC').replace(/\s+/g, '');
			const state = () => {
				const editor = document.querySelector('.chat-conversation .chat-editor');
				const vm = editor?.__vue__ || editor?.__vueParentComponent?.proxy;
				return {editor, conversation: vm?.conversation$, input: editor?.querySelector('#chat-input'), button: editor?.querySelector('.btn-send')};
			};
			const targetMatches = current => !!current.conversation && String(current.conversation.friendId) === String(request.peer_id) && current.conversation.encryptJobId === request.job_id;
			const disabled = button => !button || button.disabled || button.getAttribute('aria-disabled') === 'true' || button.classList.contains('disabled') || button.classList.contains('is-disabled');
			const waitFor = (predicate, timeout) => new Promise(resolve => {
				let timer;
				const finish = value => {observer.disconnect(); clearTimeout(timer); resolve(value);};
				const check = () => {if (predicate()) finish(true);};
				const observer = new MutationObserver(check);
				observer.observe(document.body, {subtree: true, childList: true, attributes: true, characterData: true});
				timer = setTimeout(() => finish(false), timeout);
				check();
			});
			let verification = detectVerification();
			if (verification.detected) return {...fail('BOSS verification component detected'), ...pageBlock(verification)};
			let current = state();
			if (phase === 'select') {
				if (targetMatches(current)) return {ok: true, submitted: false, stage: 'selected'};
				if (current.input?.textContent.trim()) return fail('Another conversation contains an unsent draft; it will not be overwritten');
				if (!request.company || !request.name) return fail('Target company and recruiter name required');
				const rows = [...document.querySelectorAll('li[role="listitem"]')].filter(row => normalize(row.innerText).includes(normalize(request.company)) && normalize(row.innerText).includes(normalize(request.name)));
				if (rows.length !== 1) return {...fail('The target contact cannot be identified uniquely'), code: rows.length === 0 ? 'CONTACT_NOT_LOADED' : 'CONTACT_AMBIGUOUS'};
				const contact = rows[0].querySelector('.friend-content');
				if (!contact || !contact.getClientRects().length) return fail('The target contact is unavailable');
				contact.scrollIntoView({block: 'nearest'});
				contact.click();
				if (!await waitFor(() => targetMatches(state()), 6000)) return fail('Selected conversation identity did not match the approved job');
				return {ok: true, submitted: false, stage: 'selected'};
			}
			if (!targetMatches(current)) return fail('Active recipient or job does not match');
			if (!current.input?.isConnected || !current.input.isContentEditable || !current.button?.isConnected || current.button.innerText.trim() !== '发送') return fail('The normal chat editor is not available');
			if (phase === 'prepare') {
				const draft = current.input.textContent;
				if (draft.trim() && draft !== request.text) return fail('An existing draft will not be overwritten');
				current.input.focus();
				current.input.textContent = request.text;
				current.input.dispatchEvent(new InputEvent('input', {bubbles: true, inputType: 'insertText', data: request.text}));
				current.input.dispatchEvent(new Event('change', {bubbles: true}));
				const ready = await waitFor(() => {
					const value = state();
					return targetMatches(value) && value.input?.textContent === request.text && !disabled(value.button);
				}, 3000);
				return ready ? {ok: true, submitted: false, stage: 'prepared'} : fail('The page did not enable sending after normal input');
			}
			if (phase !== 'submit') return fail('Unknown chat phase');
			current = state();
			verification = detectVerification();
			if (verification.detected) return {...fail('BOSS verification component detected'), ...pageBlock(verification)};
			if (!targetMatches(current) || current.input?.textContent !== request.text || disabled(current.button)) return fail('Recipient, draft or page permissions changed before sending');
			// This acknowledges a UI submission only; the backend must verify server history.
			submitted = true;
			current.button.click();
			return {ok: true, submitted: true, stage: 'submitted'};
		} catch (error) { return fail(String(error.message || error)); }
	}

	async function fetchPage(request) {
		const target = new URL(request.url);
		const allowedHost = location.protocol === 'https:' && ['www.zhipin.com', 'zhipin.com'].includes(location.hostname);
		if (!allowedHost || target.origin !== location.origin || target.username || target.password || !target.pathname.startsWith('/wapi/')) {
			throw new Error('Only same-origin BOSS API requests are allowed');
		}
		if (!['GET', 'POST'].includes(request.method) || (request.method === 'POST' && target.pathname !== '/wapi/zpgeek/friend/add.json')) {
			throw new Error('Only read requests and greeting submissions are supported');
		}
		const verification = detectVerification();
		if (verification.detected) {
			return {...pageBlock(verification), code: 'BRIDGE_PAGE_VERIFICATION', message: '本地检测到可见验证组件，未发起网络请求'};
		}
		const pagePath = location.pathname;
		if (/\/(?:user|login|signin|signup|register|auth)(?:\/|$)/i.test(location.pathname)) {
			return {code: 'BRIDGE_LOGIN_REQUIRED', message: '当前为登录页面，未发起网络请求', _bridge_meta: {source: 'page_login', request_sent: false}};
		}
		if (target.pathname === '/wapi/zpgeek/search/joblist.json' && !/^\/web\/geek\/jobs?\/?$/.test(location.pathname)) {
			return {code: 'BRIDGE_SEARCH_PAGE_REQUIRED', message: '当前不是职位搜索页，未发起搜索请求', _bridge_meta: {source: 'page_context', request_sent: false, page_path: location.pathname}};
		}
		const options = {
			method: request.method, credentials: 'include', redirect: 'error',
			signal: AbortSignal.timeout(25000), headers: {'Accept': 'application/json', 'X-Requested-With': 'XMLHttpRequest'},
		};
		if (request.method === 'POST') {
			options.headers['Content-Type'] = 'application/x-www-form-urlencoded';
			options.body = new URLSearchParams(request.data || {}).toString();
		}
		const response = await fetch(target.href, options);
		const data = JSON.parse(await response.text(), (_key, value, context) => {
			if (typeof value === 'number' && Number.isInteger(value) && !Number.isSafeInteger(value)) {
				if (!context?.source) throw new Error('Cannot preserve the exact numeric identifier');
				return context.source;
			}
			return value;
		});
		if (!data || typeof data !== 'object' || Array.isArray(data)) throw new Error('BOSS response is not an object');
		return {...data, _bridge_meta: {source: 'api_response', request_sent: true, http_status: response.status, endpoint: target.pathname, page_path: pagePath, verification}};
	}

	switch (action) {
		case 'inspect': return inspectPage();
		case 'inspect-search': return inspectSearch();
		case 'collect-native': return collectNative(request);
		case 'can-refresh-chat': return canRefreshChat();
		case 'inspect-chat': return inspectChat(request);
		case 'inspect-chat-identity': return inspectChatIdentity(request);
		case 'select-chat': return chatAction(request, 'select');
		case 'prepare-chat': return chatAction(request, 'prepare');
		case 'send-chat': return chatAction(request, 'submit');
		case 'fetch': return fetchPage(request);
		default: throw new Error('Unknown fixed page action');
	}
}

async function executeFixed(tabId, func, args = [], world = 'ISOLATED') {
	const deadline = Date.now() + 6000;
	let tab;
	while (true) {
		try { tab = await chrome.tabs.get(tabId); }
		catch (error) { throw pageUnavailable('BROWSER_TAB_UNAVAILABLE', 'BOSS target tab is unavailable'); }
		if (!isBossUrl(tab.url) || (tab.pendingUrl && !isBossUrl(tab.pendingUrl))) throw pageUnavailable('BROWSER_PAGE_CHANGED', 'BOSS target page changed; select the intended page again');
		if (tab.discarded) throw pageUnavailable('BROWSER_PAGE_DISCARDED', 'BOSS tab was discarded by Chrome; open it normally before continuing');
		if (tab.status !== 'loading' && !tab.pendingUrl) break;
		if (Date.now() >= deadline) throw pageUnavailable('BROWSER_PAGE_LOADING', 'BOSS page is still loading; no operation was executed');
		await new Promise(resolve => setTimeout(resolve, 200));
	}
	let results;
	try {
		results = await chrome.scripting.executeScript({target: {tabId, frameIds: [0]}, world, func, args});
	} catch (error) {
		if (/Cannot access contents of (?:the page|url)|Extension manifest must request permission|Missing host permission/i.test(error.message || '')) {
			throw pageUnavailable('BROWSER_PAGE_ACCESS_DENIED', 'Chrome denied access to the BOSS page; check extension site access or wait for page navigation to finish');
		}
		throw error;
	}
	const result = results.find(item => item.frameId === 0);
	if (!result || result.result === undefined) throw new Error('BOSS page changed or the request result is unavailable; no automatic retry');
	return result.result;
}

function pageUnavailable(code, message) {
	return Object.assign(new Error(message), {code, stage: 'before_injection', executed: false});
}

// ── Tab management ───────────────────────────────────────────────────

function isBossUrl(value) {
	try {
		const url = new URL(value);
		return url.protocol === 'https:' && (url.hostname === 'zhipin.com' || url.hostname.endsWith('.zhipin.com'));
	} catch { return false; }
}

async function resolveTabId(tabId, workspace, preferActive = false) {
	if (tabId !== undefined) {
		const tab = await chrome.tabs.get(tabId);
		if (!isBossUrl(tab.url)) throw new Error('Selected tab is not a BOSS page');
		return tabId;
	}

	// workspace="boss" 时，优先找已有的 zhipin tab（用户正常浏览的页面）
	if (workspace === 'boss') {
		// Rebind only at an idle preflight, never midway through a page command.
		if (preferActive && activeCommands <= 1) {
			const active = (await chrome.tabs.query({url: '*://*.zhipin.com/*', active: true})).filter(tab => tab.active && isBossUrl(tab.url));
			if (active.length === 1) selectedBossTabId = active[0].id;
		}
		if (selectedBossTabId !== null) {
			const previousId = selectedBossTabId;
			let selected;
			try {
				selected = await chrome.tabs.get(previousId);
			} catch (error) {
				if (!/No tab with id:/i.test(error.message || '')) throw error;
			}
			if (selected) {
				if (!isBossUrl(selected.url)) throw new Error('Selected BOSS tab changed; select a BOSS page again');
				return previousId;
			}
			// A stale ID is not evidence that the user closed the visible BOSS page.
			if (selectedBossTabId !== previousId && selectedBossTabId !== null) {
				return resolveTabId(undefined, workspace);
			}
			selectedBossTabId = null;
		}
		const zhipinTabs = await chrome.tabs.query({ url: '*://*.zhipin.com/*' });
		const candidates = zhipinTabs.filter(tab => isBossUrl(tab.url));
		const active = candidates.filter(tab => tab.active);
		const selected = candidates.length === 1 ? candidates[0] : active.length === 1 ? active[0] : null;
		if (selected) {
			selectedBossTabId = selected.id;
			return selectedBossTabId;
		}
		throw new Error(candidates.length ? 'Multiple BOSS tabs; keep one target tab open' : 'No existing BOSS tab; open BOSS manually');
	}

	throw new Error('Only the existing BOSS workspace is supported');
}

// ── WebSocket connection ─────────────────────────────────────────────

async function connect() {
	if (ws?.readyState === WebSocket.OPEN || ws?.readyState === WebSocket.CONNECTING) return;

	try {
		const res = await fetch(DAEMON_PING_URL, { signal: AbortSignal.timeout(1000) });
		if (!res.ok) return;
	} catch {
		return;
	}

	let socket;
	try {
		socket = new WebSocket(DAEMON_WS_URL);
		ws = socket;
	} catch {
		scheduleReconnect();
		return;
	}

	socket.onopen = () => {
		if (ws !== socket) { socket.close(); return; }
		console.log('[boss-bridge] Connected to daemon');
		reconnectAttempts = 0;
		if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; }
		socket.send(JSON.stringify({ type: 'hello', version: chrome.runtime.getManifest().version }));
	};

	socket.onmessage = async (event) => {
		try {
			const cmd = JSON.parse(event.data);
			if (ws !== socket) return;
			if (cmd.type === 'ready') return;
			if (cmd.type === 'ping') { socket.send(JSON.stringify({type: 'pong'})); return; }
			activeCommands++;
			try {
				const result = await handleCommand(cmd);
				if (ws === socket && socket.readyState === WebSocket.OPEN) socket.send(JSON.stringify(result));
			} finally { activeCommands--; }
		} catch (err) {
			console.error('[boss-bridge] Command error:', err);
		}
	};

	socket.onclose = () => {
		if (ws !== socket) return;
		ws = null;
		scheduleReconnect();
	};

	socket.onerror = () => socket.close();
}

function scheduleReconnect() {
	if (reconnectTimer) return;
	reconnectAttempts++;
	if (reconnectAttempts > MAX_EAGER_ATTEMPTS) return;
	const delay = Math.min(2000 * Math.pow(2, reconnectAttempts - 1), 60000);
	reconnectTimer = setTimeout(() => {
		reconnectTimer = null;
		connect();
	}, delay);
}

// ── Command dispatcher ───────────────────────────────────────────────

async function handleCommand(cmd) {
	try {
		switch (cmd.action) {
			case 'inspect': return await handleInspect(cmd);
			case 'inspect-search': {
				const tabId = await resolveTabId(cmd.tabId, cmd.workspace);
				return {id: cmd.id, ok: true, data: await executeFixed(tabId, pageCommand, ['inspect-search'], 'MAIN')};
			}
			case 'collect-native': {
				if (activeCommands > 1 || chatActionBusy) throw new Error('Another page operation is running');
				const tabId = await resolveTabId(cmd.tabId, cmd.workspace);
				const request = {...cmd.data, operationId: cmd.id, deadline: Date.now() + 20000, phase: 'start'};
				let data = await executeFixed(tabId, pageCommand, ['collect-native', request], 'MAIN');
				while (data?._native_pending === true && Date.now() < request.deadline) {
					// Extension timers are independent of background-page timer throttling.
					await new Promise(resolve => setTimeout(resolve, 200));
					data = await executeFixed(tabId, pageCommand, ['collect-native', {...request, phase: 'read'}], 'MAIN');
				}
				if (data?._native_pending === true) data = {code: 'BRIDGE_NATIVE_READ_TIMEOUT', message: '网页采集等待超时，未重复提交。', _bridge_meta: {source: 'page_context', transport: 'native-page', request_sent: null}};
				return {id: cmd.id, ok: true, data};
			}
			case 'inspect-chat': {
				const tabId = await resolveTabId(cmd.tabId, cmd.workspace);
				return {id: cmd.id, ok: true, data: await executeFixed(tabId, pageCommand, ['inspect-chat', cmd.data || {}])};
			}
			case 'inspect-chat-identity': {
				const tabId = await resolveTabId(cmd.tabId, cmd.workspace);
				return {id: cmd.id, ok: true, data: await executeFixed(tabId, pageCommand, ['inspect-chat-identity', cmd.data || {}], 'MAIN')};
			}
			case 'select-chat':
			case 'prepare-chat':
			case 'send-chat': {
				if (chatActionBusy) throw new Error('Another chat action is running');
				chatActionBusy = true;
				try {
					const tabId = await resolveTabId(cmd.tabId, cmd.workspace);
					return {id: cmd.id, ok: true, data: await executeFixed(tabId, pageCommand, [cmd.action, cmd.data || {}], 'MAIN')};
				} finally { chatActionBusy = false; }
			}

			case 'refresh-chat': {
				if (cmd.data?.confirm !== true || chatActionBusy) throw new Error('Explicit idle chat refresh required');
				chatActionBusy = true;
				try {
					const tabId = await resolveTabId(cmd.tabId, cmd.workspace);
					const check = await executeFixed(tabId, pageCommand, ['can-refresh-chat']);
					if (!check.ok) return {id: cmd.id, ok: true, data: check};
					const tab = await chrome.tabs.get(tabId);
					if (tab.url !== check.href) throw new Error('Chat page changed before refresh');
					await chrome.tabs.reload(tabId);
					return {id: cmd.id, ok: true, data: {ok: true, submitted: false, stage: 'refreshed', previousDocumentStamp: check.documentStamp}};
				} finally { chatActionBusy = false; }
			}
			case 'reload': {
				if (cmd.data?.confirm !== true || activeCommands > 1) throw new Error('Explicit local update confirmation and an idle extension are required');
				setTimeout(() => chrome.runtime.reload(), 100);
				return {id: cmd.id, ok: true, data: {reloading: true}};
			}
			case 'fetch': return await handleFetch(cmd);
			case 'exec': throw new Error('Arbitrary code execution is disabled; update the local client');
			case 'navigate': return await handleNavigate(cmd);
			case 'cookies': return await handleCookies(cmd);
			case 'close-window': return await handleCloseWindow(cmd);
			default: return { id: cmd.id, ok: false, error: `Unknown action: ${cmd.action}` };
		}
	} catch (err) {
		return { id: cmd.id, ok: false, error: err.message || String(err), code: err.code || '', stage: err.stage || '', executed: err.executed === false ? false : null };
	}
}

async function handleInspect(cmd) {
	const tabId = await resolveTabId(cmd.tabId, cmd.workspace, true);
	const data = await executeFixed(tabId, pageCommand, ['inspect']);
	data.hostPermissionGranted = await chrome.permissions.contains({origins: [`https://${data.host}/*`]});
	return { id: cmd.id, ok: true, data };
}

async function handleFetch(cmd) {
	if (typeof cmd.url !== 'string' || cmd.url.length > 16384) throw new Error('Invalid request URL');
	const target = new URL(cmd.url);
	if (!isBossUrl(target.href) || target.username || target.password || !target.pathname.startsWith('/wapi/')) throw new Error('Only BOSS API requests are allowed');
	const method = cmd.method || 'GET';
	if (!['GET', 'POST'].includes(method)) throw new Error('Unsupported request method');
	if (cmd.data !== undefined && (!cmd.data || typeof cmd.data !== 'object' || Array.isArray(cmd.data) || JSON.stringify(cmd.data).length > 65536)) throw new Error('Invalid request body');
	const tabId = await resolveTabId(cmd.tabId, cmd.workspace);
	const data = await executeFixed(tabId, pageCommand, ['fetch', {url: target.href, method, data: cmd.data || {}}]);
	return {id: cmd.id, ok: true, data};
}

async function handleNavigate(cmd) {
	throw new Error('Navigation of the existing BOSS tab is not allowed');
}

async function handleCookies(cmd) {
	return { id: cmd.id, ok: false, error: 'Cookie export is disabled; use the existing browser session' };
}

async function handleCloseWindow(cmd) {
	return { id: cmd.id, ok: true, data: { closed: false, preserved: true } };
}

// ── Lifecycle ────────────────────────────────────────────────────────

let initialized = false;

function initialize() {
	if (initialized) return;
	initialized = true;
	chrome.alarms.create('keepalive', { periodInMinutes: 0.5 });
	chrome.tabs.onRemoved.addListener((tabId) => {
		if (selectedBossTabId === tabId) selectedBossTabId = null;
	});
	chrome.tabs.onReplaced.addListener((addedTabId, removedTabId) => {
		if (selectedBossTabId === removedTabId) selectedBossTabId = addedTabId;
	});
	connect();
	console.log('[boss-bridge] Extension initialized');
}

chrome.runtime.onInstalled.addListener(initialize);
chrome.runtime.onStartup.addListener(initialize);
chrome.alarms.onAlarm.addListener((alarm) => { if (alarm.name === 'keepalive') connect(); });

// Popup status API
chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
	if (msg?.type === 'getStatus') {
		sendResponse({ connected: ws?.readyState === WebSocket.OPEN });
	}
	return false;
});

initialize();
