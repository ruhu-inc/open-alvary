# The two models D29 selected, on one GPU (a T4 holds both with room to spare): the embedder
# answers OpenAI-style /v1/embeddings on 8080 and the reranker TEI's /rerank on 8081, the APIs
# retrieval/vectors.py and retrieval/rerank.py speak. Each runs Hugging Face's text-embeddings-
# inference at the revision the development screen measured, so no query leaves Open Alvary's network.
# The region needs GPU quota for the machine size (README, "Before the first apply").

resource "azurerm_network_interface" "inference" {
  name                = "${local.name_prefix}-inference-nic"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  tags                = local.common_tags

  ip_configuration {
    name                          = "private"
    subnet_id                     = azurerm_subnet.inference.id
    private_ip_address_allocation = "Static"
    private_ip_address            = local.inference_ip
  }
}

resource "azurerm_linux_virtual_machine" "inference" {
  name                            = "${local.name_prefix}-inference"
  location                        = azurerm_resource_group.main.location
  resource_group_name             = azurerm_resource_group.main.name
  size                            = var.inference_vm_size
  admin_username                  = "openalvary"
  disable_password_authentication = true
  network_interface_ids           = [azurerm_network_interface.inference.id]
  custom_data = base64encode(templatefile("${path.module}/inference-cloud-init.yaml.tftpl", {
    image    = var.tei_image
    ip       = local.inference_ip
    embedder = var.embedder
    reranker = var.reranker
  }))
  tags = local.common_tags

  admin_ssh_key {
    username   = "openalvary"
    public_key = var.admin_ssh_public_key
  }

  os_disk {
    name                 = "${local.name_prefix}-inference-os"
    caching              = "ReadWrite"
    storage_account_type = "Premium_LRS"
    disk_size_gb         = 128 # images and model weights
  }

  source_image_reference {
    publisher = "Canonical"
    offer     = "0001-com-ubuntu-server-jammy"
    sku       = "22_04-lts-gen2"
    version   = "latest"
  }

  lifecycle {
    ignore_changes = [source_image_reference, custom_data]
  }
}

# NVIDIA's driver, installed and kept by Azure's extension.
resource "azurerm_virtual_machine_extension" "gpu_driver" {
  name                       = "NvidiaGpuDriverLinux"
  virtual_machine_id         = azurerm_linux_virtual_machine.inference.id
  publisher                  = "Microsoft.HpcCompute"
  type                       = "NvidiaGpuDriverLinux"
  type_handler_version       = "1.6"
  auto_upgrade_minor_version = true
  tags                       = local.common_tags
}
