// S1-S4 ground-truth app: every input path logs a DTA event (os_log + Documents/dta.log),
// so the host can tell an AX press (accessibilityActivate, no touches) from a synthesized touch.
import SwiftUI
import UIKit
import UserNotifications
import WebKit
import os

let dtaLog = Logger(subsystem: "dev.droidctl.testapp", category: "DTA")
let dtaFile: URL = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0].appendingPathComponent("dta.log")
let iso: ISO8601DateFormatter = { let f = ISO8601DateFormatter(); f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]; return f }()

func dta(_ event: String) {
    let line = "\(iso.string(from: Date())) DTA \(event)\n"
    dtaLog.log("DTA \(event, privacy: .public)")
    if let h = try? FileHandle(forWritingTo: dtaFile) { h.seekToEndOfFile(); h.write(line.data(using: .utf8)!); try? h.close() }
    else { try? line.write(to: dtaFile, atomically: true, encoding: .utf8) }
    DispatchQueue.main.async { status?.text = event }
}
weak var status: UILabel?

final class LoggingButton: UIButton {
    override func touchesBegan(_ t: Set<UITouch>, with e: UIEvent?) { dta("\(accessibilityIdentifier ?? "button") touch_began"); super.touchesBegan(t, with: e) }
    override func accessibilityActivate() -> Bool { dta("\(accessibilityIdentifier ?? "button") ax_activate"); return super.accessibilityActivate() }
}

/// Not a UIControl: only accessibilityActivate or a real touch can trigger it.
final class CustomView: UIView {
    override init(frame: CGRect) {
        super.init(frame: frame)
        isAccessibilityElement = true; accessibilityLabel = "Custom"; accessibilityIdentifier = "dta_custom"
        accessibilityTraits = .button; backgroundColor = .systemTeal
    }
    required init?(coder: NSCoder) { fatalError() }
    override func accessibilityActivate() -> Bool { dta("custom ax_activate"); return true }
    override func touchesEnded(_ t: Set<UITouch>, with e: UIEvent?) { dta("custom touch_ended") }
}

struct SUIControls: View {
    @State var on = false
    var body: some View {
        HStack {
            Button("SwiftUI Button") { dta("sui_button action") }.accessibilityIdentifier("dta_sui_button")
            Toggle("SwiftUI Toggle", isOn: $on).accessibilityIdentifier("dta_sui_toggle")
                .onChange(of: on) { v in dta("sui_toggle value=\(v ? 1 : 0)") }
        }
    }
}

func button(_ title: String, _ id: String, _ action: @escaping () -> Void) -> UIButton {
    let b = LoggingButton(type: .system); b.setTitle(title, for: .normal); b.accessibilityIdentifier = id
    b.addAction(UIAction { _ in dta("\(id) action"); action() }, for: .primaryActionTriggered)
    return b
}

/// Pinch target: logs the final scale of each pinch (multi-touch ground truth).
final class PinchView: UIView {
    override init(frame: CGRect) {
        super.init(frame: frame)
        backgroundColor = .systemOrange; isAccessibilityElement = true; accessibilityLabel = "Pinch"; accessibilityIdentifier = "dta_pinch"
        addGestureRecognizer(UIPinchGestureRecognizer(target: self, action: #selector(pinched(_:))))
    }
    required init?(coder: NSCoder) { fatalError() }
    @objc func pinched(_ g: UIPinchGestureRecognizer) {
        if g.state == .began { dta("pinch began touches=\(g.numberOfTouches)") }
        if g.state == .ended { dta("pinch ended scale=\(String(format: "%.2f", g.scale))") }
    }
}

final class WebHandler: NSObject, WKScriptMessageHandler {
    func userContentController(_ c: WKUserContentController, didReceive m: WKScriptMessage) { dta("web \(m.body)") }
}
let webHandler = WebHandler()
let webHTML = """
<html><head><meta name=viewport content="width=device-width"></head><body style="font:16px -apple-system">
<h3>Web section</h3>
<button id=wb onclick="webkit.messageHandlers.dta.postMessage('button click')">Web Button</button>
<input id=wi placeholder="Web input" oninput="webkit.messageHandlers.dta.postMessage('input value='+this.value)">
<a href="#x" onclick="webkit.messageHandlers.dta.postMessage('link click')">Web Link</a>
<label><input type=checkbox onchange="webkit.messageHandlers.dta.postMessage('checkbox '+this.checked)"> Web Checkbox</label>
</body></html>
"""

final class VC: UIViewController, UITableViewDataSource, UITableViewDelegate, UITextFieldDelegate {
    override func viewDidLoad() {
        super.viewDidLoad()
        view.backgroundColor = .systemBackground
        let lbl = UILabel(); lbl.text = "ready"; lbl.accessibilityIdentifier = "dta_status"; status = lbl

        let btn = button("Press me", "dta_button") {}
        let custom = CustomView()
        let sw = UISwitch(); sw.accessibilityIdentifier = "dta_switch"; sw.accessibilityLabel = "Switch"
        sw.addAction(UIAction { _ in dta("switch value=\(sw.isOn ? 1 : 0)") }, for: .valueChanged)
        let sl = UISlider(); sl.accessibilityIdentifier = "dta_slider"; sl.accessibilityLabel = "Slider"; sl.value = 0.5
        sl.addAction(UIAction { _ in dta("slider value=\(String(format: "%.2f", sl.value))") }, for: .valueChanged)
        let row1 = UIStackView(arrangedSubviews: [btn, sw, sl]); row1.spacing = 16

        let field = UITextField(); field.borderStyle = .roundedRect; field.placeholder = "Type here"; field.accessibilityIdentifier = "dta_field"
        field.autocorrectionType = .no; field.delegate = self
        field.addAction(UIAction { _ in dta("field text=\(field.text ?? "")") }, for: .editingChanged)
        field.addAction(UIAction { _ in dta("field editing_began") }, for: .editingDidBegin)

        let alert = button("Alert", "dta_alert") { [weak self] in
            let a = UIAlertController(title: "Test alert", message: "Pick one", preferredStyle: .alert)
            a.addAction(UIAlertAction(title: "OK", style: .default) { _ in dta("alert ok") })
            a.addAction(UIAlertAction(title: "Cancel", style: .cancel) { _ in dta("alert cancel") })
            self?.present(a, animated: true)
        }
        let perm = button("Permission", "dta_perm") {
            UNUserNotificationCenter.current().requestAuthorization(options: [.alert]) { granted, _ in dta("perm granted=\(granted ? 1 : 0)") }
        }
        let row2 = UIStackView(arrangedSubviews: [alert, perm]); row2.spacing = 24

        let host = UIHostingController(rootView: SUIControls()); addChild(host); host.didMove(toParent: self)

        let pinch = PinchView()
        let cfg = WKWebViewConfiguration(); cfg.userContentController.add(webHandler, name: "dta")
        let web = WKWebView(frame: .zero, configuration: cfg); web.accessibilityIdentifier = "dta_web"
        web.loadHTMLString(webHTML, baseURL: nil)
        let row3 = UIStackView(arrangedSubviews: [pinch, web]); row3.spacing = 8; row3.distribution = .fillEqually

        let table = UITableView(); table.accessibilityIdentifier = "dta_table"; table.dataSource = self; table.delegate = self
        table.register(UITableViewCell.self, forCellReuseIdentifier: "c")

        let stack = UIStackView(arrangedSubviews: [lbl, row1, custom, field, row2, host.view, row3, table]); stack.axis = .vertical; stack.spacing = 10
        stack.translatesAutoresizingMaskIntoConstraints = false; view.addSubview(stack)
        NSLayoutConstraint.activate([stack.topAnchor.constraint(equalTo: view.safeAreaLayoutGuide.topAnchor, constant: 8),
                                     stack.leadingAnchor.constraint(equalTo: view.leadingAnchor, constant: 16),
                                     stack.trailingAnchor.constraint(equalTo: view.trailingAnchor, constant: -16),
                                     stack.bottomAnchor.constraint(equalTo: view.safeAreaLayoutGuide.bottomAnchor),
                                     custom.heightAnchor.constraint(equalToConstant: 44),
                                     row3.heightAnchor.constraint(equalToConstant: 170)])
        dta("app ready")
    }
    override func viewWillTransition(to size: CGSize, with c: UIViewControllerTransitionCoordinator) {
        super.viewWillTransition(to: size, with: c); dta("rotate size=\(Int(size.width))x\(Int(size.height))")
    }
    func tableView(_ t: UITableView, numberOfRowsInSection s: Int) -> Int { 200 }
    func tableView(_ t: UITableView, cellForRowAt i: IndexPath) -> UITableViewCell {
        let c = t.dequeueReusableCell(withIdentifier: "c", for: i); c.textLabel?.text = "Item \(i.row + 1)"; return c
    }
    func tableView(_ t: UITableView, didSelectRowAt i: IndexPath) { dta("table select row=\(i.row + 1)"); t.deselectRow(at: i, animated: false) }
    func scrollViewDidEndDecelerating(_ s: UIScrollView) { dta("scroll end y=\(Int(s.contentOffset.y))") }
    func scrollViewDidEndScrollingAnimation(_ s: UIScrollView) { dta("scroll end y=\(Int(s.contentOffset.y))") }
    func scrollViewDidEndDragging(_ s: UIScrollView, willDecelerate d: Bool) { if !d { dta("scroll end y=\(Int(s.contentOffset.y))") } }
    func scrollViewWillBeginDragging(_ s: UIScrollView) { dta("scroll drag_began") }
    func textFieldShouldReturn(_ f: UITextField) -> Bool { dta("field return"); f.resignFirstResponder(); return true }
}

@main
final class AppDelegate: UIResponder, UIApplicationDelegate {
    var window: UIWindow?
    func application(_ a: UIApplication, didFinishLaunchingWithOptions o: [UIApplication.LaunchOptionsKey: Any]?) -> Bool {
        window = UIWindow(frame: UIScreen.main.bounds); window?.rootViewController = VC(); window?.makeKeyAndVisible()
        return true
    }
}
