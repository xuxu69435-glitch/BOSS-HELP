import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const source = fs.readFileSync(new URL('./background.js', import.meta.url), 'utf8');
const job = {encryptJobId: 'fixture-job', securityId: 'fixture-security', jobName: 'AI Agent工程师', brandName: 'Fixture', salaryDesc: '20-30K', cityName: '北京', jobDegree: '本科', jobExperience: '应届生', skills: ['Python']};
function fixture() {
  const calls = [];
  let context;
  let responseCode = 0;
  let responseStatus = 200;
  let ownedTransport = false;
  let siteFetch;
  const component = {isSearch: false, jobList: [], formData: {}, pageVo: {page: 1}, hasMore: true, loading: false, moreLoading: false, jobDetailLoading: false};
  const element = {isConnected: true, __vue__: component};
  const document = {title: 'Fixture', querySelector: selector => selector === '.page-jobs-main' ? element : null, querySelectorAll: () => []};
  Object.defineProperty(document, 'cookie', {get() {throw new Error('Must not inspect cookies');}});
  const headers = {'x-native-page': 'fixture'};
  const readList = async page => {
    const params = new URLSearchParams({...component.formData, page});
    const response = await (ownedTransport ? siteFetch : context.fetch)('/wapi/zpgeek/search/joblist.json?' + params, {headers});
    const data = await response.json();
    if (data.code === 0) {component.jobList = data.zpData.jobList; component.pageVo.page = page;}
  };
  component.onFilterChange = form => {component.formData = {...form, query: component.isSearch ? form.query : ''}; component.jobList = []; component.pageVo.page = 1; void readList(1);};
  component.onSearchChange = form => {component.isSearch = true; component.formData = {...component.formData, ...form};};
  component.onSearch = page => {void readList(page);};
  component.clickJobCardAction = selected => {
    component.currentJob = selected;
    component.jobDetailLoading = true;
    void (ownedTransport ? siteFetch : context.fetch)('/wapi/zpgeek/job/detail.json?securityId=' + selected.securityId, {headers}).then(response => response.json()).then(data => {
      if (data.code === 0) component.jobDetail = data.zpData;
      component.jobDetailLoading = false;
    });
  };
  class XHR {open() {} send() {} addEventListener() {}}
  context = vm.createContext({chrome: {}, document, XMLHttpRequest: XHR, URL, URLSearchParams, console, setTimeout, clearTimeout, AbortSignal, location: new URL('https://www.zhipin.com/web/geek/jobs'), performance: {timeOrigin: 1000, now: () => performance.now()}, fetch: async (url, options) => {
    calls.push({url: String(url), options});
    const detail = String(url).includes('/job/detail.json');
    const data = responseCode ? {code: responseCode, message: '您的环境存在异常.', zpData: {seed: 'secret-seed', name: 'private-challenge'}} : {code: 0, zpData: detail ? {jobInfo: {encryptId: job.encryptJobId, jobName: job.jobName, salaryDesc: job.salaryDesc, degreeName: '本科', postDescription: '2027届，本科及以上，开发Agent应用。'}, brandComInfo: {brandName: job.brandName}, bossInfo: {name: 'Fixture HR'}} : {jobList: [job], hasMore: true}};
    const text = JSON.stringify(data);
    return {status: responseStatus, json: async () => data, clone: () => ({text: async () => text})};
  }});
  vm.runInContext(source.slice(0, source.indexOf('async function executeFixed')), context);
  siteFetch = context.fetch;
  let sequence = 0;
  async function run(request, timeout = 20000) {
    const operationId = 'fixture-' + (++sequence);
    const deadline = Date.now() + timeout;
    let result = await context.pageCommand('collect-native', {...request, operationId, deadline, phase: 'start'});
    while (result?._native_pending) {
      await new Promise(resolve => setImmediate(resolve));
      result = await context.pageCommand('collect-native', {...request, operationId, deadline, phase: 'read'});
    }
    return result;
  }
  return {context, calls, component, document, setCode: code => {responseCode = code;}, setStatus: status => {responseStatus = status;}, useOwnedTransport: () => {ownedTransport = true;}, run};
}

const page = fixture();
page.context.setTimeout = () => {throw new Error('Page polling timers are forbidden');};
const params = {query: 'AI开发', city: '101010100', experience: '102', page: 1};
const list = await page.run({kind: 'search', params});
assert.equal(list.code, 0);
assert.equal(list.zpData.jobList[0].salaryDesc, '20-30K');
assert.equal(list._bridge_meta.transport, 'native-page');
assert.equal(page.calls.length, 1);
assert.equal(page.calls[0].options.headers['x-native-page'], 'fixture');
assert.equal(new URL(page.calls[0].url, 'https://www.zhipin.com').searchParams.get('query'), params.query);
const detail = await page.run({kind: 'detail', params: {securityId: job.securityId}});
assert.equal(detail.zpData.jobCard.encryptJobId, job.encryptJobId);
assert.match(detail.zpData.jobCard.postDescription, /2027/);
assert.equal(page.calls.length, 2);
const cached = await page.run({kind: 'detail', params: {securityId: job.securityId}});
assert.equal(cached.code, 0);
assert.equal(cached._bridge_meta.request_sent, false);
assert.equal(page.calls.length, 2);
const next = await page.run({kind: 'search', params: {...params, page: 2}});
assert.equal(next.code, 0);
assert.equal(page.component.pageVo.page, 2);
assert.equal(page.calls.length, 3);

const restored = await page.run({kind: 'search', params: {...params, query: 'AI Infra', page: 4}});
assert.equal(restored.code, 0);
assert.equal(page.component.pageVo.page, 4);
assert.equal(page.calls.length, 5);
assert.equal(new URL(page.calls.at(-1).url, 'https://www.zhipin.com').searchParams.get('page'), '4');
page.component.hasMore = false;
const end = await page.run({kind: 'search', params: {...params, query: 'AI Infra', page: 5}});
assert.equal(end.zpData.hasMore, false);
assert.equal(page.calls.length, 5);

const failed = fixture();
failed.setCode(37);
const risk = await failed.run({kind: 'search', params: {...params, page: 4}});
assert.equal(risk.code, 37);
assert.equal(risk._bridge_meta.source, 'api_response');
assert.equal(failed.calls.length, 1, 'Do not request page 4 after a blocked context restoration');
assert.ok(!JSON.stringify(risk).includes('secret-seed'));
assert.ok(!JSON.stringify(failed.context.__bossAgentNativeCapture.entries).includes('secret-seed'));
const serverError = fixture();
serverError.setStatus(500);
assert.equal((await serverError.run({kind: 'search', params})).code, 'BRIDGE_NATIVE_HTTP_ERROR');
assert.equal(serverError.calls.length, 1);
const owned = fixture();
owned.useOwnedTransport();
owned.context.setTimeout = () => {throw new Error('No page polling');};
const ownedList = await owned.run({kind: 'search', params});
assert.equal(ownedList.code, 0);
assert.equal(ownedList._bridge_meta.source, 'page_state');
assert.equal(ownedList._bridge_meta.request_sent, null);
assert.equal(owned.context.__bossAgentNativeCapture.entries.length, 0);
const ownedDetail = await owned.run({kind: 'detail', params: {securityId: job.securityId}});
assert.equal(ownedDetail.zpData.jobCard.encryptJobId, job.encryptJobId);
assert.equal(ownedDetail._bridge_meta.request_sent, null);
owned.setCode(37);
const unconfirmed = await owned.run({kind: 'search', params}, 40);
assert.equal(unconfirmed.code, 'BRIDGE_NATIVE_READ_TIMEOUT');
assert.equal(owned.calls.length, 3, 'Do not retry an unconfirmed page read');

const cachedJob = fixture();
const fromSaved = await cachedJob.run({kind: 'detail', params: {securityId: job.securityId, nativeJob: job}});
assert.equal(fromSaved.code, 0);
assert.equal(fromSaved.zpData.jobCard.encryptJobId, job.encryptJobId);
assert.equal(cachedJob.calls.length, 1);
cachedJob.component.jobDetail.jobInfo.encryptId = 'wrong-job';
const wrongIdentity = await cachedJob.run({kind: 'detail', params: {securityId: job.securityId, nativeJob: job}});
assert.equal(wrongIdentity.code, 'BRIDGE_NATIVE_DETAIL_UNAVAILABLE');
assert.equal(cachedJob.calls.length, 1);

const invalid = fixture();
const unsupported = await invalid.run({kind: 'search', params: {...params, arbitrary: 'ignored'}});
assert.equal(unsupported.code, 'BRIDGE_NATIVE_PARAMETERS');
assert.equal(invalid.calls.length, 0);
invalid.context.location = new URL('https://www.zhipin.com/web/geek/chat');
assert.equal((await invalid.run({kind: 'search', params})).code, 'BRIDGE_SEARCH_PAGE_REQUIRED');
assert.equal(invalid.calls.length, 0);
console.log('Native collection: search, JD identity, reuse, pagination, restoration and code 37 checks passed.');
