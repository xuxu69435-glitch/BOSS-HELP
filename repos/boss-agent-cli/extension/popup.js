chrome.runtime.sendMessage({ type: 'getStatus' }, (res) => {
	const dot = document.getElementById('dot');
	const label = document.getElementById('label');
	const hint = document.getElementById('hint');
	if (res?.connected) {
		dot.classList.add('on');
		label.textContent = '已连接本机服务';
		hint.textContent = `扩展版本：${chrome.runtime.getManifest().version}`;
	} else {
		dot.classList.add('off');
		label.textContent = '未连接';
		hint.textContent = '本机桥接服务未启动。';
	}
});
