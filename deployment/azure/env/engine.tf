# Vespa, one node to start (the owner's choice, RSV-04): its private address only, its index on a
# data disk, the application deployed and the units fed by the retrieval job (retrieval.projector),
# rebuilt from PostgreSQL whenever it is replaced. A second node and a content cluster per large
# jurisdiction come when the corpus needs them (retrieval/engine.py, CLUSTERS). Off (engine_enabled)
# while the environment stands by: the service then searches in PostgreSQL, as it does with no engine.

resource "azurerm_network_interface" "engine" {
  count               = var.engine_enabled ? 1 : 0
  name                = "${local.name_prefix}-engine-nic"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  tags                = local.common_tags

  ip_configuration {
    name                          = "private"
    subnet_id                     = azurerm_subnet.engine.id
    private_ip_address_allocation = "Static"
    private_ip_address            = local.engine_ip
  }
}

resource "azurerm_linux_virtual_machine" "engine" {
  count                           = var.engine_enabled ? 1 : 0
  name                            = "${local.name_prefix}-engine"
  location                        = azurerm_resource_group.main.location
  resource_group_name             = azurerm_resource_group.main.name
  size                            = var.engine_vm_size
  admin_username                  = "openalvary"
  disable_password_authentication = true
  network_interface_ids           = [azurerm_network_interface.engine[0].id]
  custom_data = base64encode(templatefile("${path.module}/engine-cloud-init.yaml.tftpl", {
    image = var.vespa_image
    ip    = local.engine_ip
  }))
  tags = local.common_tags

  admin_ssh_key {
    username   = "openalvary"
    public_key = var.admin_ssh_public_key
  }

  os_disk {
    name                 = "${local.name_prefix}-engine-os"
    caching              = "ReadWrite"
    storage_account_type = "Premium_LRS"
  }

  source_image_reference {
    publisher = "Canonical"
    offer     = "0001-com-ubuntu-server-jammy"
    sku       = "22_04-lts-gen2"
    version   = "latest"
  }

  lifecycle {
    ignore_changes = [source_image_reference, custom_data] # a new image or start-up script never replaces the machine silently
  }
}

resource "azurerm_managed_disk" "engine" {
  count                = var.engine_enabled ? 1 : 0
  name                 = "${local.name_prefix}-engine-data"
  location             = azurerm_resource_group.main.location
  resource_group_name  = azurerm_resource_group.main.name
  storage_account_type = "Premium_LRS"
  create_option        = "Empty"
  disk_size_gb         = var.engine_disk_gb
  tags                 = local.common_tags
}

resource "azurerm_virtual_machine_data_disk_attachment" "engine" {
  count              = var.engine_enabled ? 1 : 0
  managed_disk_id    = azurerm_managed_disk.engine[0].id
  virtual_machine_id = azurerm_linux_virtual_machine.engine[0].id
  lun                = 0
  caching            = "None"
}
